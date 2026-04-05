#!/usr/bin/env python3
"""Cron-driven pipeline orchestrator for HyperLiquid trading."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import time
import argparse
import uuid
from datetime import datetime, timezone

import yaml

from scanner import load_config, scan_universe, get_btc_4h_change
from signal_scorer import score_candidates
from entry_engine import check_entry_conditions
from execute import open_position
from exit_monitor import check_exits
from reconcile import reconcile_positions
from positions import count_open_positions


RUN_LOG_FILE = Path(__file__).parent.parent / "data" / "run_log.jsonl"


def _now_utc_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_data_dir():
    """Ensure data directory exists."""
    DATA_DIR = Path(__file__).parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Nansen usage tracking
USAGE_LOG = Path(__file__).parent.parent / "data" / "nansen_usage_log.jsonl"


def _log_run_summary(run_data: dict) -> None:
    """Append run summary to run_log.jsonl."""
    _ensure_data_dir()
    with open(RUN_LOG_FILE, "a") as f:
        f.write(json.dumps(run_data) + "\n")


def notify(msg: str) -> None:
    """Send Telegram message using env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
    import os
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        print(f"[TELEGRAM SKIPPED] Env vars not set: TELEGRAM_BOT_TOKEN={bool(bot_token)}, TELEGRAM_CHAT_ID={bool(chat_id)}")
        return
    
    import urllib.request
    import urllib.parse
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
    
    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print(f"[TELEGRAM SENT] {msg[:100]}...")
    except Exception as e:
        print(f"[TELEGRAM ERROR] Failed to send: {e}")


def run_pipeline(dry_run: bool = False) -> dict:
    """
    Execute the trading pipeline.
    
    Args:
        dry_run: If True, skip actual execution (no trades placed)
    
    Returns:
        dict with run summary data
    """
    start_time = time.time()
    errors = []
    trades_executed = 0
    exits_triggered = 0
    tokens_scanned = 0
    signals_scored = 0
    
    try:
        config = load_config()
    except Exception as e:
        errors.append(f"Config load failed: {e}")
        config = {}
    
    # Check BTC kill switch
    btc_change = 0.0
    btc_kill_switch_active = False
    try:
        btc_change = get_btc_4h_change()
        btc_kill_switch_pct = config.get("btc_kill_switch_pct", -0.03)
        if btc_change < btc_kill_switch_pct:
            btc_kill_switch_active = True
            errors.append(f"BTC kill switch triggered: {btc_change:.2f}% < {btc_kill_switch_pct*100:.2f}%")
    except Exception as e:
        errors.append(f"BTC change check failed: {e}")
    
    # Stage: Exit monitor (ALWAYS runs)
    try:
        exit_actions = check_exits(config)
        exits_triggered = len([a for a in exit_actions if a.get("action") == "closed"])
    except Exception as e:
        errors.append(f"Exit monitor failed: {e}")
    
    # Check max positions
    positions_open = count_open_positions()
    max_positions = config.get("max_positions", 4)
    max_positions_reached = positions_open >= max_positions
    
    # Stage: Scanner (skip if max positions reached)
    candidates = []
    if not max_positions_reached:
        try:
            candidates = scan_universe(config)
            tokens_scanned = len(candidates)
        except Exception as e:
            errors.append(f"Scanner failed: {e}")
    
    # Stage: Score candidates
    scored_candidates = []
    if candidates:
        try:
            scored_candidates = score_candidates(candidates, config)
            signals_scored = len(scored_candidates)
        except Exception as e:
            errors.append(f"Score candidates failed: {e}")
    
    # Stage: Entry engine and execution (loop candidates until max_positions filled)
    if not max_positions_reached and not btc_kill_switch_active and scored_candidates:
        entry_score_threshold = config.get("entry_score_threshold", 60)
        for candidate in scored_candidates:
            # Stop if we've reached max positions
            if count_open_positions() >= max_positions:
                break

            score = candidate.get("score", 0)
            if score < entry_score_threshold:
                break  # Sorted by score descending — rest won't qualify either

            symbol = candidate.get("symbol")
            direction = candidate.get("direction", "long").upper()

            try:
                entry_result = check_entry_conditions(symbol, direction, config)
                if entry_result.get("confirmed"):
                    if dry_run:
                        print(f"[DRY RUN] Would open {direction} position on {symbol} (score={score})")
                        trades_executed += 1
                    else:
                        result = open_position(symbol, candidate.get("asset_index"), direction, candidate.get("mark_price"), config)
                        if "error" not in result:
                            trades_executed += 1
                        else:
                            errors.append(f"Open position failed for {symbol}: {result.get('error')}")
            except Exception as e:
                errors.append(f"Entry check failed for {symbol}: {e}")
    
    # Stage: Reconcile (always run)
    try:
        reconcile_positions(config)
    except Exception as e:
        errors.append(f"Reconcile failed: {e}")
    
    duration_s = time.time() - start_time
    
    run_data = {
        "run_id": str(uuid.uuid4()),
        "ts": _now_utc_iso(),
        "duration_s": round(duration_s, 2),
        "tokens_scanned": tokens_scanned,
        "signals_scored": signals_scored,
        "trades_executed": trades_executed,
        "exits_triggered": exits_triggered,
        "errors": errors,
        "btc_change": round(btc_change, 2),
        "positions_open": count_open_positions()
    }
    
    _log_run_summary(run_data)

    # Nansen usage log
    _ensure_data_dir()
    usage_entry = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'run_id': str(uuid.uuid4()),
        'ts': datetime.now(timezone.utc).isoformat(),
        'tokens_scanned_nansen': tokens_scanned,
        'credits_used': -1,  # HyperLiquid does not track Nansen credits
        'run_duration_s': round(duration_s, 2),
    }
    with open(USAGE_LOG, 'a') as f:
        f.write(json.dumps(usage_entry) + '\n')
    
    # Send Telegram notification only if something notable happened
    error_count = len(errors)
    if trades_executed > 0 or exits_triggered > 0 or error_count > 0:
        summary_parts = ["[HL TRADER]"]
        if trades_executed > 0:
            summary_parts.append(f"✅ {trades_executed} trade(s) opened")
        if exits_triggered > 0:
            summary_parts.append(f"📤 {exits_triggered} exit(s) triggered")
        if error_count > 0:
            summary_parts.append(f"⚠️ {error_count} error(s): {'; '.join(errors[:2])}")
        notify("\n".join(summary_parts))
    
    return run_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the trading pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Run without executing trades")
    args = parser.parse_args()
    
    run_pipeline(dry_run=args.dry_run)
