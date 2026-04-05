"""Exit monitoring for open positions with trailing stops and kill switches."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
from datetime import datetime, timezone, timedelta
from hl_client import get_client, get_info_client, get_funding_history
from positions import get_open_positions, close_position
import yaml


EXIT_MONITOR_LOG_FILE = Path(__file__).parent.parent / "data" / "exit_monitor_log.jsonl"
ALERT_CACHE_FILE = Path(__file__).parent.parent / "data" / "exit_alerts_cache.json"


def _ensure_data_dir():
    DATA_DIR = Path(__file__).parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _load_config():
    """Load configuration from config/config.yaml."""
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _log_exit_check(event: dict) -> None:
    """Append exit check event to exit_monitor_log.jsonl."""
    _ensure_data_dir()
    with open(EXIT_MONITOR_LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def _load_alert_cache() -> dict:
    """Load alert cache from file."""
    if not ALERT_CACHE_FILE.exists():
        return {}
    with open(ALERT_CACHE_FILE, "r") as f:
        return json.load(f)


def _save_alert_cache(cache: dict) -> None:
    """Save alert cache to file."""
    _ensure_data_dir()
    with open(ALERT_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _should_send_alert(symbol: str, condition: str) -> bool:
    """Check if we should send alert for this symbol+condition combo."""
    cache = _load_alert_cache()
    now = _now_utc_timestamp()
    thirty_minutes_ago = now - (30 * 60)
    
    key = f"{symbol}:{condition}"
    if key in cache:
        last_alert = cache[key]
        if last_alert > thirty_minutes_ago:
            return False
    
    cache[key] = now
    _save_alert_cache(cache)
    return True


def _get_current_funding_rate(coin: str) -> float:
    """Get current funding rate for a coin."""
    try:
        info, _, _ = get_client()
        funding_history = info.funding_history(coin, int(_now_utc_timestamp() * 1000) - 86400000)
        if funding_history and len(funding_history) > 0:
            return funding_history[0].get("rate", 0.0)
        return 0.0
    except Exception:
        return 0.0


def update_peak_trough(position: dict, current_price: float) -> dict:
    """Updates peak_price or trough_price in position record and saves to positions.json."""
    direction = position.get("direction")
    updated = position.copy()
    
    if direction == "LONG":
        peak_price = position.get("peak_price", current_price)
        if current_price > peak_price:
            updated["peak_price"] = current_price
    elif direction == "SHORT":
        trough_price = position.get("trough_price", current_price)
        if current_price < trough_price:
            updated["trough_price"] = current_price
    
    # Save updated position
    positions = get_open_positions()
    # Need to reload all positions to update the right one
    from positions import load_positions
    all_positions = load_positions()
    for i, p in enumerate(all_positions):
        if p.get("id") == position.get("id"):
            all_positions[i] = updated
            break
    from positions import save_positions
    save_positions(all_positions)
    
    return updated


def check_exits(config: dict) -> list:
    """
    Load open positions from positions.py, check all exit conditions,
    and close positions if triggered. Returns list of actions taken.
    """
    actions = []
    open_positions = get_open_positions()
    
    for position in open_positions:
        symbol = position.get("symbol")
        direction = position.get("direction")
        entry_price = position.get("entry_price")
        position_id = position.get("id")
        
        # Get current mark price from hl_client
        try:
            info_client = get_info_client()
            meta, asset_contexts = info_client.meta_and_asset_ctxs()
            universe = meta.get("universe", [])
            current_price = None
            for asset_info, ctx in zip(universe, asset_contexts):
                if asset_info.get("name") == symbol:
                    try:
                        current_price = float(ctx.get("markPx") or 0)
                    except (TypeError, ValueError):
                        current_price = None
                    break
            if not current_price:
                continue
        except Exception:
            continue
        
        # Update peak/trough
        position = update_peak_trough(position, current_price)
        
        # Check exit conditions in order
        exit_reason = None
        
        # a) STOP LOSS
        if exit_reason is None:
            if direction == "LONG" and current_price <= position.get("stop_loss"):
                exit_reason = "stop_loss"
            elif direction == "SHORT" and current_price >= position.get("stop_loss"):
                exit_reason = "stop_loss"
        
        # b) TAKE PROFIT
        if exit_reason is None:
            if direction == "LONG" and current_price >= position.get("take_profit"):
                exit_reason = "take_profit"
            elif direction == "SHORT" and current_price <= position.get("take_profit"):
                exit_reason = "take_profit"
        
        # c) TIME EXIT
        if exit_reason is None:
            max_hold_until = position.get("max_hold_until")
            if max_hold_until and _now_utc_timestamp() >= max_hold_until:
                exit_reason = "time_exit"
        
        # d) FUNDING DRAIN
        if exit_reason is None:
            funding_kill_switch_pct = config.get("funding_kill_switch_pct", 0.0005)
            funding_rate = _get_current_funding_rate(symbol)
            
            # Check if position has been open > 4 hours
            opened_at = position.get("opened_at_utc")
            if opened_at:
                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                hours_open = (_now_utc_timestamp() - opened_dt.timestamp()) / 3600
                if hours_open > 4:
                    if direction == "LONG" and funding_rate > funding_kill_switch_pct:
                        exit_reason = "funding_drain"
                    elif direction == "SHORT" and funding_rate < -funding_kill_switch_pct:
                        exit_reason = "funding_drain"
        
        # e) TRAILING STOP
        if exit_reason is None:
            trailing_activation = position.get("trailing_activation", 0)
            trailing_stop_trail_pct = config.get("trailing_stop_trail_pct", 0.03)
            peak_price = position.get("peak_price", entry_price)
            trough_price = position.get("trough_price", entry_price)
            
            trailing_active = False
            if direction == "LONG":
                if current_price > peak_price:
                    peak_price = current_price
                if current_price >= entry_price * (1 + trailing_activation):
                    trailing_active = True
                if trailing_active and current_price < peak_price * (1 - trailing_stop_trail_pct):
                    exit_reason = "trailing_stop"
            elif direction == "SHORT":
                if current_price < trough_price:
                    trough_price = current_price
                if current_price <= entry_price * (1 - trailing_activation):
                    trailing_active = True
                if trailing_active and current_price > trough_price * (1 + trailing_stop_trail_pct):
                    exit_reason = "trailing_stop"
        
        # Log each check
        conditions_checked = ["stop_loss", "take_profit", "time_exit", "funding_drain", "trailing_stop"]
        for condition in conditions_checked:
            triggered = (exit_reason == condition)
            _log_exit_check({
                "ts": _now_utc_iso(),
                "symbol": symbol,
                "direction": direction,
                "current_price": current_price,
                "condition_checked": condition,
                "triggered": triggered,
                "reason": exit_reason if triggered else None
            })
        
        # If exit triggered, close position
        if exit_reason:
            try:
                from execute import close_position_market
                result = close_position_market(position, exit_reason, config)
                if result.get("status") == "success":
                    actions.append({
                        "position_id": position_id,
                        "symbol": symbol,
                        "direction": direction,
                        "exit_reason": exit_reason,
                        "exit_price": result.get("exit_price"),
                        "action": "closed"
                    })
            except Exception as e:
                actions.append({
                    "position_id": position_id,
                    "symbol": symbol,
                    "direction": direction,
                    "exit_reason": exit_reason,
                    "error": str(e),
                    "action": "failed"
                })
    
    return actions
