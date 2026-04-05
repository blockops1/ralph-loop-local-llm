#!/usr/bin/env python3
"""Post-trade health monitoring for HyperLiquid perp trader."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import yaml
from datetime import datetime, timezone, timedelta
from hl_client import get_user_state, get_open_orders
from positions import get_open_positions
import os


DATA_DIR = Path(__file__).parent.parent / "data"
RUN_LOG_FILE = DATA_DIR / "run_log.jsonl"
HEALTH_CHECK_LOG_FILE = DATA_DIR / "health_check_log.jsonl"


def _now_utc_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _load_config():
    """Load configuration from config/config.yaml."""
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _log_health_check(result: dict) -> None:
    """Append health check result to health_check_log.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_CHECK_LOG_FILE, "a") as f:
        f.write(json.dumps(result) + "\n")


def _notify_telegram(message: str) -> None:
    """Send notification via Telegram."""
    config = _load_config()
    if not config.get("notifications", {}).get("enabled", False):
        return
    # Telegram notification logic would go here
    # For now, just log to console
    print(f"[TELEGRAM] {message}")


def _get_last_run_log_entry() -> dict | None:
    """Get the most recent run_log entry."""
    if not RUN_LOG_FILE.exists():
        return None
    with open(RUN_LOG_FILE, "r") as f:
        lines = f.readlines()
        if not lines:
            return None
        last_line = lines[-1].strip()
        if not last_line:
            return None
        return json.loads(last_line)


def _count_errors_last_24h() -> int:
    """Count errors in run_log from last 24 hours."""
    if not RUN_LOG_FILE.exists():
        return 0
    errors = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with open(RUN_LOG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_str = entry.get("ts", "")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
                if entry.get("error"):
                    errors += 1
            except json.JSONDecodeError:
                continue
    return errors


def run_health_check(config: dict | None = None) -> dict:
    """
    Run health checks on pipeline and position state.
    
    Args:
        config: Configuration dict (loads from config.yaml if None)
        
    Returns:
        dict with keys: healthy (bool), checks (list of {name, passed, detail})
    """
    if config is None:
        config = _load_config()
    
    checks = []
    healthy = True
    
    # a) PIPELINE FRESHNESS
    last_entry = _get_last_run_log_entry()
    freshness_passed = True
    freshness_detail = ""
    if last_entry is None:
        freshness_passed = False
        freshness_detail = "No run_log entries found"
        healthy = False
    else:
        ts_str = last_entry.get("ts", "")
        if not ts_str:
            freshness_passed = False
            freshness_detail = "run_log entry missing timestamp"
            healthy = False
        else:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                if age_hours > 5:
                    freshness_passed = False
                    freshness_detail = f"Last run was {age_hours:.1f} hours ago (threshold: 5h)"
                    healthy = False
                else:
                    freshness_detail = f"Last run was {age_hours:.1f} hours ago"
            except Exception:
                freshness_passed = False
                freshness_detail = "Failed to parse run_log timestamp"
                healthy = False
    
    checks.append({
        "name": "PIPELINE FRESHNESS",
        "passed": freshness_passed,
        "detail": freshness_detail
    })
    
    # b) POSITION VERIFICATION
    user_state = get_user_state()
    on_chain_positions = {}
    if user_state and "positions" in user_state:
        for pos in user_state["positions"]:
            if pos and pos.get("szi") != "0":
                asset_name = pos.get("assetName", "")
                on_chain_positions[asset_name] = pos
    
    local_positions = get_open_positions()
    local_by_asset = {p.get("asset"): p for p in local_positions}
    
    position_passed = True
    position_detail = ""
    mismatched_assets = []
    
    for asset_name, local_pos in local_by_asset.items():
        if asset_name not in on_chain_positions:
            position_passed = False
            mismatched_assets.append(asset_name)
    
    if position_passed:
        position_detail = "All local positions exist on-chain"
    else:
        position_detail = f"Missing on-chain positions for: {', '.join(mismatched_assets)}"
        healthy = False
    
    checks.append({
        "name": "POSITION VERIFICATION",
        "passed": position_passed,
        "detail": position_detail
    })
    
    # c) WALLET BALANCE
    min_account_value = config.get("min_account_value", 50)
    account_value = float(user_state.get("accountSummary", {}).get("accountValue", "0"))
    
    balance_passed = account_value >= min_account_value
    balance_detail = f"Account value: {account_value:.2f} USDC (threshold: {min_account_value} USDC)"
    
    if not balance_passed:
        healthy = False
    
    checks.append({
        "name": "WALLET BALANCE",
        "passed": balance_passed,
        "detail": balance_detail
    })
    
    # d) ORDER VERIFICATION
    open_orders = get_open_orders()
    on_chain_orders = {}
    for order in open_orders:
        asset_idx = order.get("assetIndex")
        order_id = order.get("orderId")
        on_chain_orders[(asset_idx, order_id)] = order
    
    order_passed = True
    order_detail = ""
    missing_orders = []
    
    for local_pos in local_positions:
        asset_index = local_pos.get("asset_index")
        # Check for TP and SL orders
        # Orders are identified by asset_index and type (tp/sl)
        # We need to find orders that match this position
        found_tp = False
        found_sl = False
        
        for (idx, oid), order in on_chain_orders.items():
            if idx == asset_index:
                order_type = order.get("type", {})
                if "trigger" in order_type:
                    tpsl = order_type.get("tpsl", "")
                    if tpsl == "tp":
                        found_tp = True
                    elif tpsl == "sl":
                        found_sl = True
        
        if not found_tp:
            order_passed = False
            missing_orders.append(f"{local_pos.get('asset')} TP")
        if not found_sl:
            order_passed = False
            missing_orders.append(f"{local_pos.get('asset')} SL")
    
    if order_passed:
        order_detail = "All TP/SL orders exist on-chain"
    else:
        order_detail = f"Missing orders: {', '.join(missing_orders)}"
        healthy = False
    
    checks.append({
        "name": "ORDER VERIFICATION",
        "passed": order_passed,
        "detail": order_detail
    })
    
    # e) ERROR RATE
    error_count = _count_errors_last_24h()
    max_errors = 3
    error_passed = error_count <= max_errors
    error_detail = f"Errors in last 24h: {error_count} (threshold: {max_errors})"
    
    if not error_passed:
        healthy = False
    
    checks.append({
        "name": "ERROR RATE",
        "passed": error_passed,
        "detail": error_detail
    })
    
    result = {
        "healthy": healthy,
        "checks": checks,
        "timestamp": _now_utc_iso()
    }
    
    _log_health_check(result)
    
    # Send Telegram alerts for failed checks
    for check in checks:
        if not check["passed"]:
            _notify_telegram(f"Health check failed: {check['name']} - {check['detail']}")
    
    return result


def auto_fix(check_result: dict, config: dict | None = None) -> list:
    """
    Attempt to auto-fix health check issues.
    
    Args:
        check_result: Result from run_health_check
        config: Configuration dict (loads from config.yaml if None)
        
    Returns:
        list of actions taken
    """
    if config is None:
        config = _load_config()
    
    actions = []
    
    # Get on-chain orders
    open_orders = get_open_orders()
    on_chain_orders = {}
    for order in open_orders:
        asset_idx = order.get("assetIndex")
        order_id = order.get("orderId")
        on_chain_orders[(asset_idx, order_id)] = order
    
    # Get local positions
    local_positions = get_open_positions()
    
    for check in check_result.get("checks", []):
        if check["name"] == "ORDER VERIFICATION" and not check["passed"]:
            detail = check.get("detail", "")
            # Parse missing orders from detail string
            if "Missing orders:" in detail:
                missing_str = detail.split("Missing orders:")[1].strip()
                missing_items = [m.strip() for m in missing_str.split(",")]
                
                for item in missing_items:
                    if "TP" in item:
                        asset = item.replace(" TP", "").strip()
                        # Find position by asset
                        for pos in local_positions:
                            if pos.get("asset") == asset:
                                asset_index = pos.get("asset_index")
                                take_profit = pos.get("take_profit")
                                size = pos.get("size")
                                direction = pos.get("direction")
                                
                                if asset_index and take_profit and size:
                                    # Re-place TP order
                                    is_buy = direction == "SHORT"
                                    order_type = {
                                        "trigger": {
                                            "triggerCondition": "markPrice",
                                            "triggerPrice": take_profit
                                        },
                                        "tpsl": "tp"
                                    }
                                    from hl_client import place_order
                                    place_order(asset_index, is_buy, size, take_profit, order_type)
                                    actions.append(f"Re-placed TP order for {asset}")
                                    _notify_telegram(f"Auto-fix: Re-placed TP order for {asset}")
                                    break
                    elif "SL" in item:
                        asset = item.replace(" SL", "").strip()
                        # Find position by asset
                        for pos in local_positions:
                            if pos.get("asset") == asset:
                                asset_index = pos.get("asset_index")
                                stop_loss = pos.get("stop_loss")
                                size = pos.get("size")
                                direction = pos.get("direction")
                                
                                if asset_index and stop_loss and size:
                                    # Re-place SL order
                                    is_buy = direction == "SHORT"
                                    order_type = {
                                        "trigger": {
                                            "triggerCondition": "markPrice",
                                            "triggerPrice": stop_loss
                                        },
                                        "tpsl": "sl"
                                    }
                                    from hl_client import place_order
                                    place_order(asset_index, is_buy, size, stop_loss, order_type)
                                    actions.append(f"Re-placed SL order for {asset}")
                                    _notify_telegram(f"Auto-fix: Re-placed SL order for {asset}")
                                    break
    
    return actions


if __name__ == '__main__':
    config = _load_config()
    check_result = run_health_check(config)
    print(f"Health check result: {'HEALTHY' if check_result['healthy'] else 'UNHEALTHY'}")
    for check in check_result['checks']:
        status = "PASS" if check['passed'] else "FAIL"
        print(f"  [{status}] {check['name']}: {check['detail']}")
    
    if not check_result['healthy']:
        actions = auto_fix(check_result, config)
        if actions:
            print(f"Auto-fix actions taken: {len(actions)}")
            for action in actions:
                print(f"  - {action}")
