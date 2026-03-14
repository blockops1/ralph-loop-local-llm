#!/usr/bin/env python3
"""
run_pipeline.py — end-to-end pipeline orchestrator

Orchestrates the full trading pipeline:
1. Discover smart money tokens
2. Load existing positions
3. For each token: fetch data, calculate SR, score signal
4. Log discovery, signals, and trades
5. Update positions on BUY

DATA DIRECTORY: base_dir/data/ — created if it does not exist.
FOUR DATA FILES — written every run:
  data/positions.json      — current open positions (read + rewrite each run)
  data/signal_log.jsonl    — one appended line per token evaluated
  data/trade_log.jsonl     — written by decision.log_trade() on BUY
  data/discovery_log.jsonl — one appended line per pipeline run

CLI:
  python3 run_pipeline.py --mock
  python3 run_pipeline.py --mock --wallet-balance 50
  python3 run_pipeline.py --paper --wallet-balance 50   # Live data, no real trades
"""

import json
import os
import sys
import uuid
import argparse
import requests
from datetime import datetime
from pathlib import Path

# Set up paths
base_dir = Path(__file__).parent.parent
data_dir = base_dir / "data"
os.makedirs(data_dir, exist_ok=True)

# Add scripts/ directory to path so sibling modules can be imported
sys.path.insert(0, str(Path(__file__).parent))

from token_scanner import get_price_ohlcv, get_sm_netflow, get_sm_dex_trades, get_top_holders, get_pool_liquidity, discover_sm_tokens
from support_resistance import calculate_sr
from signal_filter import score_signal
from decision import format_decision, log_trade


def send_telegram_message(message: str) -> bool:
    """Send a message to Telegram via the Bot API."""
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    
    if not bot_token or not chat_id:
        print("[TELEGRAM] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env var")
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message
    }
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return r.json().get('ok', False)
    except requests.RequestException as e:
        print(f"[TELEGRAM] Failed to send message: {e}")
        return False


def load_settings():
    """Load settings from config/settings.json."""
    config_path = base_dir / "config" / "settings.json"
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {}


def load_positions() -> list[dict]:
    """Load existing positions from data/positions.json."""
    positions_path = data_dir / "positions.json"
    if positions_path.exists():
        with open(positions_path, 'r') as f:
            return json.load(f)
    return []


def save_positions(positions: list[dict]) -> None:
    """Save positions to data/positions.json."""
    positions_path = data_dir / "positions.json"
    with open(positions_path, 'w') as f:
        json.dump(positions, f, indent=2)


def write_discovery_log(run_id: str, run_ts: str, discovered_tokens: list[dict]) -> None:
    """Write ONE line to data/discovery_log.jsonl."""
    log_path = data_dir / "discovery_log.jsonl"
    record = {
        "ts": run_ts,
        "run_id": run_id,
        "tokens_found": len(discovered_tokens),
        "tokens": [
            {
                "symbol": t['symbol'],
                "holders_count": t.get('holders_count', 0),
                "volume_24h": t.get('volume_24h', 0)
            }
            for t in discovered_tokens
        ]
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')


def write_signal_log(run_id: str, run_ts: str, scored: dict, token: dict, action_taken: str) -> None:
    """Write ONE line to data/signal_log.jsonl."""
    log_path = data_dir / "signal_log.jsonl"
    record = {
        "ts": run_ts,
        "run_id": run_id,
        "symbol": scored['symbol'],
        "address": token['address'],
        "score": scored['total_score'],
        "passes": scored['passes'],
        "disqualified": scored['disqualified'],
        "disqualify_reason": scored['disqualify_reason'],
        "pillars": scored['pillars'],
        "holders_count": token.get('holders_count', 0),
        "volume_24h": token.get('volume_24h', 0),
        "action_taken": action_taken
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')


def print_summary_table(results: list[dict]) -> None:
    """Print summary table of all evaluated tokens."""
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"{'symbol':<10} | {'holders':>7} | {'score':>5} | {'passes':>6} | {'action':>12}")
    print("-" * 80)
    for r in results:
        print(f"{r['symbol']:<10} | {r['holders']:>7} | {r['score']:>5} | {str(r['passes']):>6} | {r['action']:>12}")
    print("=" * 80)


def print_trade_details(trades: list[dict]) -> None:
    """Print full trade dict for each passing BUY."""
    if not trades:
        print("No trades executed.")
        return
    print("\nTRADES EXECUTED:")
    print("-" * 40)
    for trade in trades:
        print(json.dumps(trade, indent=2))
    print("-" * 40)


def main():
    """Main pipeline execution."""
    # Parse CLI arguments
    parser = argparse.ArgumentParser(description='End-to-end trading pipeline orchestrator')
    parser.add_argument('--mock', action='store_true', help='Enable mock mode (NANSEN_MOCK=1)')
    parser.add_argument('--paper', action='store_true', help='Paper trading mode: live data, no real trades')
    parser.add_argument('--wallet-balance', type=float, default=50.0, help='Wallet balance in USD (default: 50.0)')
    args = parser.parse_args()

    # Set modes
    paper_mode = args.paper
    if args.mock:
        os.environ['NANSEN_MOCK'] = '1'
        paper_mode = True  # Mock implies paper

    wallet_balance = args.wallet_balance
    btc_change_4h = 0.5  # Hardcoded for Phase 1

    # Generate run ID and timestamp
    run_id = str(uuid.uuid4())[:8]
    run_ts = datetime.utcnow().isoformat() + 'Z'

    mode_str = "PAPER" if paper_mode else "LIVE"
    print(f"[PIPELINE] Run ID: {run_id}")
    print(f"[PIPELINE] Mode: {mode_str}")
    print(f"[PIPELINE] Timestamp: {run_ts}")
    print(f"[PIPELINE] Wallet Balance: ${wallet_balance:.2f}")
    print(f"[PIPELINE] BTC Change 4h: {btc_change_4h}%")

    # Step 1: Load settings
    settings = load_settings()
    print(f"[PIPELINE] Settings loaded: {len(settings)} keys")

    # Step 2: Discover smart money tokens
    print("[PIPELINE] Discovering smart money tokens...")
    discovered_tokens = discover_sm_tokens(chain='base', limit=20)

    if not discovered_tokens:
        print("[PIPELINE] No SM tokens discovered")
        sys.exit(0)

    print(f"[PIPELINE] Discovered {len(discovered_tokens)} tokens")

    # Step 3: Load existing positions
    positions = load_positions()
    open_symbols = {p['symbol'] for p in positions if p['status'] == 'open'}
    print(f"[PIPELINE] Loaded {len(positions)} positions, {len(open_symbols)} open symbols")

    # Step 4: Write discovery log
    write_discovery_log(run_id, run_ts, discovered_tokens)
    print(f"[PIPELINE] Discovery log written")

    # Step 5: Process each token
    results = []
    trades = []

    for token in discovered_tokens:
        symbol = token['symbol']
        address = token['address']
        print(f"\n[PIPELINE] Processing {symbol} ({address})...")

        # 5a: Get price OHLCV
        candles = get_price_ohlcv(address)
        if not candles or len(candles) == 0:
            print(f"[PIPELINE] {symbol}: No price data, skipping")
            # Create a minimal scored result for logging
            scored = {
                'symbol': symbol,
                'total_score': 0,
                'passes': False,
                'disqualified': True,
                'disqualify_reason': 'no_price_data',
                'pillars': {'price_structure': 0, 'smart_money': 0, 'volume': 0, 'token_health': 0}
            }
            action_taken = 'SKIP_NO_DATA'
            write_signal_log(run_id, run_ts, scored, token, action_taken)
            results.append({
                'symbol': symbol,
                'holders': token.get('holders_count', 0),
                'score': 0,
                'passes': False,
                'action': action_taken
            })
            continue

        # 5b: Calculate support/resistance
        sr = calculate_sr(candles)
        if sr is None:
            print(f"[PIPELINE] {symbol}: No SR data, skipping")
            scored = {
                'symbol': symbol,
                'total_score': 0,
                'passes': False,
                'disqualified': True,
                'disqualify_reason': 'no_sr_data',
                'pillars': {'price_structure': 0, 'smart_money': 0, 'volume': 0, 'token_health': 0}
            }
            action_taken = 'SKIP_NO_SR'
            write_signal_log(run_id, run_ts, scored, token, action_taken)
            results.append({
                'symbol': symbol,
                'holders': token.get('holders_count', 0),
                'score': 0,
                'passes': False,
                'action': action_taken
            })
            continue

        # 5c: Get smart money netflow
        netflow = get_sm_netflow(address)

        # 5d: Get smart money DEX trades
        trades_data = get_sm_dex_trades(address)

        # 5e: Get top holders
        holders = get_top_holders(address)

        # 5f: Build pool data
        pool = {
            'tvl_usd': token.get('volume_24h', 0.0),
            'meets_minimum': token.get('volume_24h', 0.0) >= 100_000
        }

        # 5g: Build full token dict
        token_full = {
            **token,
            'chain': 'base',
            'min_liquidity_usd': 100_000.0
        }

        # 5h: Score the signal
        scored = score_signal(
            token_full, sr, netflow, trades_data, holders, pool,
            btc_change_4h=btc_change_4h, list_type='smart_money'
        )

        # 5i: Determine action_taken
        if symbol in open_symbols:
            action_taken = 'SKIP_OPEN'
        elif scored['passes']:
            action_taken = 'BUY'
        else:
            action_taken = 'SKIP_FAIL'

        # 5j: Write signal log
        write_signal_log(run_id, run_ts, scored, token, action_taken)

        # Add to results for summary
        results.append({
            'symbol': symbol,
            'holders': token.get('holders_count', 0),
            'score': scored['total_score'],
            'passes': scored['passes'],
            'action': action_taken
        })

        # 5k: Execute BUY if applicable
        if action_taken == 'BUY':
            print(f"[PIPELINE] {symbol}: BUY signal!")

            # Format the trade decision
            trade = format_decision(scored, wallet_balance)

            # In paper mode, mark as paper trade and skip real execution
            if paper_mode:
                trade['paper'] = True
                print(f"[PIPELINE] {symbol}: PAPER TRADE — not executing on-chain")

            # Log the trade
            log_trade(trade, str(data_dir))

            # Append to positions list
            positions.append({
                "symbol": trade['symbol'],
                "address": address,
                "opened_at": run_ts,
                "entry_price": trade['entry_price'],
                "position_usd": trade['position_usd'],
                "stop_loss": trade['stop_loss'],
                "target": trade['target'],
                "score": trade['score'],
                "status": "open",
                "thesis": trade['thesis']
            })

            # Rewrite positions file
            save_positions(positions)

            # Add to trades list for summary
            trades.append(trade)

            print(f"[PIPELINE] {symbol}: Trade logged, positions updated")

            # Send Telegram alert for BUY signal
            entry_price = trade['entry_price']
            score = scored['total_score']
            support = sr.get('support', 0.0)
            resistance = sr.get('resistance', 0.0)
            telegram_msg = f"🟢 BUY {symbol} @ {entry_price:.4f} | score={score} | support={support:.4f} resistance={resistance:.4f}"
            send_telegram_message(telegram_msg)

        else:
            print(f"[PIPELINE] {symbol}: {action_taken} (score={scored['total_score']}, passes={scored['passes']})")

    # Step 6: Print summary table
    print_summary_table(results)

    # Print trade details
    print_trade_details(trades)

    print(f"\n[PIPELINE] Run {run_id} complete.")
    print(f"[PIPELINE] Total tokens processed: {len(results)}")
    print(f"[PIPELINE] Trades executed: {len(trades)}")

    sys.exit(0)


if __name__ == '__main__':
    main()
