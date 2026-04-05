"""On-chain position reconciliation module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
from datetime import datetime, timezone
from hl_client import get_user_state
from positions import load_positions, save_positions, close_position


RECONCILE_LOG_FILE = Path(__file__).parent.parent / "data" / "reconcile_log.jsonl"


def _now_utc_iso() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _log_reconcile_event(event_type: str, details: dict) -> None:
    """Log a reconciliation event to reconcile_log.jsonl."""
    RECONCILE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": _now_utc_iso(),
        "event_type": event_type,
        **details
    }
    with open(RECONCILE_LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def reconcile_positions(config: dict) -> dict:
    """
    Reconcile local position state against HyperLiquid on-chain state.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        dict with keys: matched, orphaned, unknown, mismatched, pnl_updated
    """
    result = {
        "matched": 0,
        "orphaned": 0,
        "unknown": 0,
        "mismatched": 0,
        "pnl_updated": 0
    }
    
    # Get on-chain positions from HyperLiquid
    user_state = get_user_state()
    on_chain_positions = {}
    
    if user_state and "positions" in user_state:
        for pos in user_state["positions"]:
            if pos and pos.get("szi") != "0":
                asset_name = pos.get("assetName", "")
                on_chain_positions[asset_name] = pos
    
    # Load local positions
    local_positions = load_positions()
    local_open_positions = [p for p in local_positions if p.get("status") == "open"]
    
    # Create lookup by asset name
    local_by_asset = {p.get("asset"): p for p in local_open_positions}
    
    # Track which local positions have been matched
    matched_local_ids = set()
    
    for asset_name, on_chain_pos in on_chain_positions.items():
        if asset_name in local_by_asset:
            local_pos = local_by_asset[asset_name]
            local_id = local_pos.get("id")
            matched_local_ids.add(local_id)
            
            on_chain_size = float(on_chain_pos.get("szi", "0"))
            local_size = local_pos.get("size", 0)
            
            # Check for size mismatch
            if abs(on_chain_size - local_size) > 1e-8:
                result["mismatched"] += 1
                _log_reconcile_event("size_mismatch", {
                    "position_id": local_id,
                    "asset": asset_name,
                    "local_size": local_size,
                    "on_chain_size": on_chain_size
                })
            
            # Update unrealized PnL from on-chain data
            on_chain_unrealized_pnl = float(on_chain_pos.get("unrealizedPnl", "0"))
            local_unrealized_pnl = local_pos.get("unrealized_pnl", 0)
            
            if abs(on_chain_unrealized_pnl - local_unrealized_pnl) > 1e-8:
                local_pos["unrealized_pnl"] = on_chain_unrealized_pnl
                result["pnl_updated"] += 1
                _log_reconcile_event("pnl_updated", {
                    "position_id": local_id,
                    "asset": asset_name,
                    "old_pnl": local_unrealized_pnl,
                    "new_pnl": on_chain_unrealized_pnl
                })
            
            result["matched"] += 1
        else:
            # Unknown on-chain position
            result["unknown"] += 1
            _log_reconcile_event("unknown_on_chain_position", {
                "asset": asset_name,
                "size": on_chain_pos.get("szi"),
                "unrealized_pnl": on_chain_pos.get("unrealizedPnl")
            })
    
    # Find orphaned local positions (local says open, not on-chain)
    for local_pos in local_open_positions:
        local_id = local_pos.get("id")
        asset = local_pos.get("asset")
        
        if local_id not in matched_local_ids:
            result["orphaned"] += 1
            _log_reconcile_event("orphaned_position", {
                "position_id": local_id,
                "asset": asset,
                "size": local_pos.get("size")
            })
            # Close locally with reason='reconcile_orphan'
            close_position(local_id, 0.0, "reconcile_orphan")
    
    # Save updated positions
    save_positions(local_positions)
    
    return result


def get_account_summary(config: dict) -> dict:
    """
    Return account summary for position sizing and daily reports.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        dict with keys: account_value, total_margin_used, withdrawable,
                       total_unrealized_pnl, open_position_count
    """
    user_state = get_user_state()
    
    if not user_state:
        return {
            "account_value": 0.0,
            "total_margin_used": 0.0,
            "withdrawable": 0.0,
            "total_unrealized_pnl": 0.0,
            "open_position_count": 0
        }
    
    # Extract account summary from user state
    account_summary = user_state.get("accountSummary", {})
    account_value = float(account_summary.get("accountValue", "0"))
    total_margin_used = float(account_summary.get("totalMarginUsed", "0"))
    withdrawable = float(account_summary.get("withdrawable", "0"))
    
    # Calculate total unrealized PnL from positions
    total_unrealized_pnl = 0.0
    open_position_count = 0
    
    if "positions" in user_state:
        for pos in user_state["positions"]:
            if pos and pos.get("szi") != "0":
                open_position_count += 1
                unrealized_pnl = float(pos.get("unrealizedPnl", "0"))
                total_unrealized_pnl += unrealized_pnl
    
    return {
        "account_value": account_value,
        "total_margin_used": total_margin_used,
        "withdrawable": withdrawable,
        "total_unrealized_pnl": total_unrealized_pnl,
        "open_position_count": open_position_count
    }
