"""
decision.py - trade thesis formatter

This module provides functions to format trading decisions and log trades.
"""

import json
import os
from datetime import datetime


def format_decision(scored: dict, wallet_balance: float, pool_tvl: float = 0.0,
                   max_position_pct: float = 0.20, pool_cap_pct: float = 0.01,
                   stop_loss_pct: float = 0.92) -> dict:
    """
    Format a trading decision for signals that pass all filters.
    
    Only call for signals where scored['passes'] is True.
    
    Strategy constants (max_position_pct, pool_cap_pct, stop_loss_pct) default to
    live values and should be overridden from config for testing.
    
    Parameters:
        scored: dict from score_signal() with 'passes': True
        wallet_balance: float - current wallet balance in USD
        pool_tvl: float - pool TVL in USD (default 0.0, optional)
        max_position_pct: float - max position as fraction of wallet (default 0.20 = 20%)
        pool_cap_pct: float - max position as fraction of pool TVL (default 0.01 = 1%)
        stop_loss_pct: float - stop loss as fraction of entry (default 0.92 = 8% down)
    
    Returns:
        dict with trade details including entry, stop loss, target, and thesis
    """
    
    # Position size = min(max_position_pct of wallet, pool_cap_pct of pool TVL)
    wallet_position = wallet_balance * max_position_pct
    if pool_tvl > 0:
        pool_cap = pool_tvl * pool_cap_pct
        position_usd = min(wallet_position, pool_cap)
    else:
        position_usd = wallet_position
    
    # Entry price from SR summary
    entry_price = scored['sr_summary']['current_price']
    
    # Stop loss = stop_loss_pct below entry (default 8% below for SM system)
    stop_loss = entry_price * stop_loss_pct
    
    # Target = resistance level (defensive: default to entry * 1.10 if missing)
    target = scored['sr_summary'].get('resistance') or (entry_price * 1.10 if entry_price else 0)
    
    # Build thesis string (no LLM call)
    symbol = scored['symbol']
    support_price = scored['sr_summary'].get('support', entry_price * 0.95)
    pct_above_support = scored['sr_summary'].get('pct_above_support', 0)
    sm_trader_count = scored['pillars'].get('sm_trader_count', 0)
    sm_buy_volume = scored['pillars'].get('sm_buy_volume_usd', 0)
    score = scored['total_score']
    
    thesis = f"{symbol} near support at ${support_price:.6f} ({pct_above_support}% above). {sm_trader_count} SM wallets accumulated ${sm_buy_volume:,.0f} in 24h. Score: {score}/{scored.get('max_score', 400)}."
    
    return {
        'symbol': symbol,
        'list_type': 'smart_money',
        'action': 'BUY',
        'entry_price': entry_price,
        'position_usd': position_usd,
        'stop_loss': stop_loss,
        'target': target,
        'score': score,
        'thesis': thesis
    }


def log_trade(trade: dict, data_dir: str) -> None:
    """
    Append a trade record to the trade log file.
    
    Parameters:
        trade: dict with trade details
        data_dir: str - directory path where trade_log.jsonl should be stored
    """
    
    # Create the log file path
    log_path = os.path.join(data_dir, 'trade_log.jsonl')
    
    # Add timestamp and event type
    record = trade.copy()
    record['ts'] = datetime.utcnow().isoformat() + 'Z'
    record['event'] = 'BUY'
    
    # Append to file (create if doesn't exist)
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')
