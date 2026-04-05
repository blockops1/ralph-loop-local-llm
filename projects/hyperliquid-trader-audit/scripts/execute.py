"""Order execution for long and short perpetual positions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import uuid
from datetime import datetime, timezone
import yaml

from hl_client import get_client, get_meta, get_user_state, place_order, cancel_order
from positions import add_position, close_position
import os


TRADE_LOG_FILE = Path(__file__).parent.parent / "data" / "trade_log.jsonl"


def _ensure_data_dir():
    DATA_DIR = Path(__file__).parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config():
    """Load configuration from config/config.yaml."""
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _log_trade_event(event: dict) -> None:
    """Append trade event to trade_log.jsonl."""
    _ensure_data_dir()
    with open(TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def _notify_telegram(message: str) -> None:
    """Send notification via Telegram (pattern from hl_client)."""
    config = _load_config()
    if not config.get("notifications", {}).get("enabled", False):
        return
    # Telegram notification logic would go here
    # For now, just log to console
    print(f"[TELEGRAM] {message}")


def calculate_position_size(account_value: float, price: float, leverage: int, config: dict) -> float:
    """Calculate position size based on config and account value."""
    position_notional = config.get("position_size_usd", 100)
    
    # Verify: position_notional <= account_value * 0.20 (max 20% of account per position)
    max_position_notional = account_value * 0.20
    if position_notional > max_position_notional:
        position_notional = max_position_notional
    
    actual_size = position_notional / price
    
    # Get asset decimals from metadata
    meta = get_meta()
    asset_contexts = meta.get("universe", [])
    sz_decimals = 8  # default
    for asset in asset_contexts:
        if asset.get("name") == "BTC" or asset.get("name") == "ETH":  # Simplified lookup
            sz_decimals = asset.get("szDecimals", 8)
            break
    
    # Round to asset's szDecimals
    decimal_places = sz_decimals
    actual_size = round(actual_size, decimal_places)
    
    return actual_size


def calculate_risk_prices(entry_price: float, direction: str, config: dict) -> dict:
    """Calculate stop loss and take profit prices based on direction."""
    stop_loss_pct = config.get("stop_loss_pct", 0.08)
    take_profit_pct = config.get("take_profit_pct", 0.15)
    trailing_activation = config.get("trailing_stop_activation_pct", 0.06)
    max_hold_until_hours = config.get("max_hold_hours", 72)
    
    if direction == "LONG":
        stop_loss = entry_price * (1 - stop_loss_pct)
        take_profit = entry_price * (1 + take_profit_pct)
    elif direction == "SHORT":
        stop_loss = entry_price * (1 + stop_loss_pct)
        take_profit = entry_price * (1 - take_profit_pct)
    else:
        raise ValueError(f"Invalid direction: {direction}")
    
    # Calculate max_hold_until timestamp
    max_hold_until = datetime.now(timezone.utc).timestamp() + (max_hold_until_hours * 3600)
    
    return {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trailing_activation": trailing_activation,
        "max_hold_until": max_hold_until
    }


def open_position(symbol: str, asset_index: int, direction: str, price: float, config: dict) -> dict:
    """Open a new position with stop loss and take profit orders."""
    try:
        # Get user state for account value
        user_state = get_user_state()
        # SDK returns account value under marginSummary.accountValue
        account_value = float(
            user_state.get("marginSummary", {}).get("accountValue")
            or user_state.get("accountValue")
            or 1000
        )
        
        # Calculate position size
        leverage = config.get("max_leverage", 5)
        size = calculate_position_size(account_value, price, leverage, config)
        
        # Calculate risk prices
        risk_prices = calculate_risk_prices(price, direction, config)
        
        # Determine if paper trade
        paper_trade = config.get("paper_trade", False)
        
        # Place market order
        is_buy = direction == "LONG"
        order_type = {"limit": {"price": price, "tif": "Gtc"}}
        
        order_result = place_order(asset_index, is_buy, size, price, order_type)
        
        if order_result.get("status") not in ("ok", "success"):
            return {"error": f"Failed to place order: {order_result}"}
        
        order_id = order_result.get("orderId", str(uuid.uuid4()))
        
        # Place stop loss order (trigger order with tpsl='sl')
        sl_price = risk_prices["stop_loss"]
        sl_order_type = {
            "trigger": {
                "triggerCondition": "markPrice",
                "triggerPrice": sl_price
            },
            "tpsl": "sl"
        }
        place_order(asset_index, not is_buy, size, sl_price, sl_order_type)
        
        # Place take profit order (trigger order with tpsl='tp')
        tp_price = risk_prices["take_profit"]
        tp_order_type = {
            "trigger": {
                "triggerCondition": "markPrice",
                "triggerPrice": tp_price
            },
            "tpsl": "tp"
        }
        place_order(asset_index, not is_buy, size, tp_price, tp_order_type)
        
        # Record position
        position_record = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "asset_index": asset_index,
            "direction": direction,
            "entry_price": price,
            "size": size,
            "account_value": account_value,
            "leverage": leverage,
            "stop_loss": risk_prices["stop_loss"],
            "take_profit": risk_prices["take_profit"],
            "trailing_activation": risk_prices["trailing_activation"],
            "max_hold_until": risk_prices["max_hold_until"],
            "order_id": order_id,
            "status": "open",
            "opened_at_utc": _now_utc_iso(),
            "paper": paper_trade
        }
        
        add_position(position_record)
        
        # Log to trade_log.jsonl
        trade_event = {
            "ts": _now_utc_iso(),
            "symbol": symbol,
            "direction": direction,
            "action": "open",
            "price": price,
            "size": size,
            "order_id": order_id,
            "paper": paper_trade,
            "reason": "signal"
        }
        _log_trade_event(trade_event)
        
        # Notify via Telegram
        _notify_telegram(f"Position opened: {symbol} {direction} at {price}")
        
        return position_record
        
    except Exception as e:
        return {"error": str(e)}


def close_position_market(position: dict, reason: str, config: dict) -> dict:
    """Close a position using market order."""
    try:
        asset_index = position.get("asset_index")
        direction = position.get("direction")
        size = position.get("size")
        position_id = position.get("id")
        
        paper_trade = config.get("paper_trade", False)
        
        # Place market order in opposite direction (reduceOnly=True)
        is_buy = direction == "SHORT"  # Opposite of position direction
        order_type = {"market": {}}
        
        order_result = place_order(asset_index, is_buy, size, 0, order_type, reduce_only=True)
        
        if not order_result.get("status") == "success":
            return {"error": f"Failed to close position: {order_result}"}
        
        exit_price = order_result.get("price", 0)
        order_id = order_result.get("orderId", str(uuid.uuid4()))
        
        # Cancel any open TP/SL orders for this position
        # Get open orders and cancel those related to this position
        from hl_client import get_open_orders
        open_orders = get_open_orders()
        for order in open_orders:
            if order.get("assetIndex") == asset_index:
                cancel_order(asset_index, order.get("orderId"))
        
        # Update position via positions.close_position
        close_position(position_id, exit_price, reason)
        
        # Log to trade_log.jsonl
        trade_event = {
            "ts": _now_utc_iso(),
            "symbol": position.get("symbol"),
            "direction": direction,
            "action": "close",
            "price": exit_price,
            "size": size,
            "order_id": order_id,
            "paper": paper_trade,
            "reason": reason
        }
        _log_trade_event(trade_event)
        
        # Notify via Telegram
        _notify_telegram(f"Position closed: {position.get('symbol')} {reason}")
        
        return {
            "status": "success",
            "position_id": position_id,
            "exit_price": exit_price,
            "order_id": order_id
        }
        
    except Exception as e:
        return {"error": str(e)}
