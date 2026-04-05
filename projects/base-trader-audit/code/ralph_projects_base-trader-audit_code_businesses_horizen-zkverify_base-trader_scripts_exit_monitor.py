#!/usr/bin/env python3
"""
exit_monitor.py - automated position exit monitor

Runs every 60 minutes via launchd. For each open position in data/positions.json:
  - Fetches current price via Nansen screener (scanner.get_screener_data)
  - Checks exit conditions: stop_loss, target, time_limit (3 days)
  - If triggered: calls execute.sell_token(), logs to trade_log.jsonl,
    updates positions.json, sends Telegram alert

Usage:
  python3 scripts/exit_monitor.py
  python3 scripts/exit_monitor.py --dry-run   # checks but does not sell
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add scripts/ to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

# Load .env
_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip().replace('export ', '').strip(), _v.strip())

from scanner import get_screener_data, get_price_ohlcv
from execute import sell_token

TIME_LIMIT_DAYS = 14
STOP_LOSS_PCT = 0.08  # 8% below entry — applied to both new positions and partial-exit remaining


def send_telegram(message: str) -> None:
    """Send a Telegram message via bot API to the trading group."""
    import requests
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    group_id = '-5264050975'  # Trading group
    if not token or not group_id:
        print(f'[MONITOR] Telegram not configured, skipping: {message[:80]}')
        return
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        requests.post(url, json={'chat_id': group_id, 'text': message}, timeout=10)
        print('[MONITOR] Telegram alert sent')
    except Exception as e:
        print(f'[MONITOR] Telegram error: {e}')


def get_current_price(address: str) -> float:
    """
    Get current token price in USD.
    Primary: Nansen screener price_usd.
    Fallback: most recent OHLCV close.
    Returns 0.0 if unavailable.
    """
    try:
        screener = get_screener_data(address)
        if screener and screener.get('price_usd'):
            return float(screener['price_usd'])
    except Exception:
        pass
    try:
        candles = get_price_ohlcv(address)
        if candles:
            return float(candles[-1].get('close', 0) or 0)
    except Exception:
        pass
    return 0.0


def log_trade_event(event: dict, log_path: str) -> None:
    """Append a trade event to trade_log.jsonl."""
    with open(log_path, 'a') as f:
        f.write(json.dumps(event) + '\n')


def get_tranches(position: dict) -> list:
    """
    Get tranches for a position. Backward compatible: if no 'tranches' field,
    treat as single tranche [{pct: 1.0, status: 'open'}].
    """
    tranches = position.get('tranches')
    if tranches is None:
        return [{'pct': 1.0, 'status': 'open'}]
    return tranches


def get_total_open_pct(tranches: list) -> float:
    """Sum pct of all tranches with status 'open'."""
    return sum(t.get('pct', 0) for t in tranches if t.get('status') == 'open')


def check_exits(positions_path: str, log_path: str, dry_run: bool = False) -> list:
    """
    Check all open positions for exit conditions.
    Returns list of exit events that fired.
    """
    try:
        with open(positions_path) as f:
            positions = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print('[MONITOR] No positions.json - nothing to monitor')
        return []

    open_positions = [p for p in positions if p.get('status') in ('open', 'partial_exit')]
    if not open_positions:
        print('[MONITOR] No open positions')
        return []

    print(f'[MONITOR] Checking {len(open_positions)} open position(s)')
    exits = []
    now = datetime.now(timezone.utc)

    for pos in open_positions:
        symbol = pos.get('symbol', '?')
        address = pos.get('address', '')
        entry_price = float(pos.get('entry_price', 0))
        stop_loss = float(pos.get('stop_loss', 0))
        target = float(pos.get('target', 0))
        opened_at_str = pos.get('opened_at', '')

        current_price = get_current_price(address)
        if current_price == 0.0:
            print(f'[MONITOR] {symbol}: could not get price, skipping')
            continue

        # Calculate unrealized P&L
        pct_change = ((current_price - entry_price) / entry_price * 100) if entry_price else 0

        # Get tranches and trailing_stop
        tranches = get_tranches(pos)
        trailing_stop = pos.get('trailing_stop')

        # Determine exit reason
        is_partial = pos.get('status') == 'partial_exit'
        exit_reason = None
        if stop_loss > 0 and current_price <= stop_loss:
            exit_reason = 'stop_loss'
        elif target > 0 and current_price >= target and not is_partial:
            # partial_exit positions only honor stop_loss/trailing_stop, not target_hit again
            exit_reason = 'target_hit'
        elif opened_at_str:
            try:
                opened_at = datetime.fromisoformat(opened_at_str.replace('Z', '+00:00'))
                age_days = (now - opened_at).total_seconds() / 86400
                if age_days >= TIME_LIMIT_DAYS:
                    exit_reason = 'time_limit'
            except ValueError:
                pass

        # Check trailing_stop condition (new)
        if trailing_stop is not None and current_price <= trailing_stop:
            exit_reason = 'trailing_stop'

        print(f'[MONITOR] {symbol}: price=${current_price:.6f}  entry=${entry_price:.6f}  '
              f'pnl={pct_change:+.1f}%  stop=${stop_loss:.6f}  target=${target:.6f}  '
              f'reason={exit_reason or "hold"}')

        if exit_reason is None:
            # Update trailing_stop if price > entry and trailing_stop exists
            if trailing_stop is not None and current_price > entry_price:
                new_trailing_stop = max(trailing_stop, current_price * 0.95)
                pos['trailing_stop'] = new_trailing_stop
            continue

        # Execute SELL
        print(f'[MONITOR] {symbol}: EXIT triggered ({exit_reason})')

        # Determine sell_pct based on exit reason
        total_open_pct = get_total_open_pct(tranches)
        sell_pct = 1.0  # default: sell all

        if exit_reason == 'target_hit':
            # Sell 50% (first tranche)
            sell_pct = 0.5
            # Update tranches: mark first open tranche as partial_exit, reduce its pct
            for t in tranches:
                if t.get('status') == 'open':
                    t['status'] = 'partial_exit'
                    t['pct'] = t.get('pct', 1.0) * 0.5
                    break
            # Move stop_loss on remaining to 8% below entry (preserve buffer, not breakeven)
            pos['stop_loss'] = entry_price * (1 - STOP_LOSS_PCT)
            # Set new trailing_stop on remaining 50%
            pos['trailing_stop'] = current_price * 0.95
            # Update position status
            pos['status'] = 'partial_exit'
            pos['partial_exit_at'] = now.isoformat()
            # Log partial exit
            event = {
                'event': 'PARTIAL_EXIT',
                'symbol': symbol,
                'address': address,
                'exit_reason': 'target_hit',
                'entry_price': entry_price,
                'exit_price': current_price,
                'sell_pct': 0.5,
                'remaining_pct': total_open_pct * 0.5,
                'pnl_pct': round(pct_change, 2),
                'ts': now.isoformat(),
                'dry_run': dry_run
            }
            if not dry_run:
                log_trade_event(event, log_path)
            # Telegram alert sent AFTER sell confirmation (see below)

        elif exit_reason == 'stop_loss':
            # Sell ALL remaining tranches
            sell_pct = total_open_pct
            # Update all tranches to closed
            for t in tranches:
                if t.get('status') == 'open':
                    t['status'] = 'closed'
            # Update position status
            pos['status'] = 'closed'
            pos['closed_at'] = now.isoformat()
            pos['close_price'] = current_price
            pos['close_reason'] = 'stop_loss'
            pos['pnl_pct'] = round(pct_change, 2)
            # Log to trade_log
            event = {
                'event': 'SELL',
                'symbol': symbol,
                'address': address,
                'exit_reason': 'stop_loss',
                'entry_price': entry_price,
                'exit_price': current_price,
                'pnl_pct': round(pct_change, 2),
                'pnl_usd': round(pos.get('position_usd', 0) * pct_change / 100, 2),
                'sell_pct': sell_pct,
                'tx_hash': None,
                'sell_status': None,
                'ts': now.isoformat(),
                'dry_run': dry_run
            }
            # Telegram alert
            pass  # Telegram alert sent after sell confirmation below

        elif exit_reason == 'time_limit':
            # Sell ALL remaining tranches
            sell_pct = total_open_pct
            # Update all tranches to closed
            for t in tranches:
                if t.get('status') == 'open':
                    t['status'] = 'closed'
            # Update position status
            pos['status'] = 'closed'
            pos['closed_at'] = now.isoformat()
            pos['close_price'] = current_price
            pos['close_reason'] = 'time_limit'
            pos['pnl_pct'] = round(pct_change, 2)
            # Log to trade_log
            event = {
                'event': 'SELL',
                'symbol': symbol,
                'address': address,
                'exit_reason': 'time_limit',
                'entry_price': entry_price,
                'exit_price': current_price,
                'pnl_pct': round(pct_change, 2),
                'pnl_usd': round(pos.get('position_usd', 0) * pct_change / 100, 2),
                'sell_pct': sell_pct,
                'tx_hash': None,
                'sell_status': None,
                'ts': now.isoformat(),
                'dry_run': dry_run
            }
            # Telegram alert
            pass  # Telegram alert sent after sell confirmation below

        elif exit_reason == 'trailing_stop':
            # Sell ALL remaining tranches
            sell_pct = total_open_pct
            # Update all tranches to closed
            for t in tranches:
                if t.get('status') == 'open':
                    t['status'] = 'closed'
            # Update position status
            pos['status'] = 'closed'
            pos['closed_at'] = now.isoformat()
            pos['close_price'] = current_price
            pos['close_reason'] = 'trailing_stop'
            pos['pnl_pct'] = round(pct_change, 2)
            # Log to trade_log
            event = {
                'event': 'SELL',
                'symbol': symbol,
                'address': address,
                'exit_reason': 'trailing_stop',
                'entry_price': entry_price,
                'exit_price': current_price,
                'pnl_pct': round(pct_change, 2),
                'pnl_usd': round(pos.get('position_usd', 0) * pct_change / 100, 2),
                'sell_pct': sell_pct,
                'tx_hash': None,
                'sell_status': None,
                'ts': now.isoformat(),
                'dry_run': dry_run
            }
            # Telegram alert
            pass  # Telegram alert sent after sell confirmation below

        else:
            # Default: sell all (backward compatible)
            sell_pct = total_open_pct
            # Update all tranches to closed
            for t in tranches:
                if t.get('status') == 'open':
                    t['status'] = 'closed'
            # Update position status
            pos['status'] = 'closed'
            pos['closed_at'] = now.isoformat()
            pos['close_price'] = current_price
            pos['close_reason'] = exit_reason
            pos['pnl_pct'] = round(pct_change, 2)
            # Log to trade_log
            event = {
                'event': 'SELL',
                'symbol': symbol,
                'address': address,
                'exit_reason': exit_reason,
                'entry_price': entry_price,
                'exit_price': current_price,
                'pnl_pct': round(pct_change, 2),
                'pnl_usd': round(pos.get('position_usd', 0) * pct_change / 100, 2),
                'sell_pct': sell_pct,
                'tx_hash': None,
                'sell_status': None,
                'ts': now.isoformat(),
                'dry_run': dry_run
            }
            # Telegram alert
            pass  # Telegram alert sent after sell confirmation below

        # Execute sell if not dry_run
        if not dry_run:
            sell_result = sell_token(address, sell_pct=sell_pct)
            sell_status = sell_result.get('status', 'failed')
            tx_hash = sell_result.get('tx_hash')
        else:
            sell_status = 'dry_run'
            tx_hash = None
            print(f'[MONITOR] {symbol}: DRY-RUN - would sell {sell_pct*100:.0f}% at ${current_price:.6f}')

        # Update event and position with sell result
        event['tx_hash'] = tx_hash
        event['sell_status'] = sell_status
        if pos.get('status') != 'partial_exit':
            pos['close_tx'] = tx_hash

        # Only alert on confirmed sell (or dry_run) — suppress on failed/reverted
        sell_confirmed = sell_status in ('confirmed', 'dry_run')
        if not sell_confirmed:
            print(f'[MONITOR] {symbol}: sell {sell_status} — skipping Telegram alert')
        elif exit_reason == 'target_hit':
            # Send deferred PARTIAL EXIT alert now that sell is confirmed
            pnl_emoji = '✅' if pct_change >= 0 else '🔴'
            msg = (
                f"{pnl_emoji} (PARTIAL) EXIT: {symbol}\n"
                f"Reason: target_hit\n"
                f"Entry: ${entry_price:.6f} -> Exit: ${current_price:.6f}\n"
                f"P&L: {pct_change:+.1f}%\n"
                f"Sold 50%, remaining 50% at breakeven stop\n"
                f"TX: {'dry-run' if dry_run else tx_hash or 'pending'}"
            )
            send_telegram(msg)
        else:
            # Full exit alert (stop_loss, time_limit, trailing_stop, etc.)
            pnl_emoji = '✅' if pct_change >= 0 else '🔴'
            msg = (
                f"{pnl_emoji} EXIT: {symbol}\n"
                f"Reason: {exit_reason}\n"
                f"Entry: ${entry_price:.6f} -> Exit: ${current_price:.6f}\n"
                f"P&L: {pct_change:+.1f}%\n"
                f"TX: {'dry-run' if dry_run else tx_hash or 'pending'}"
            )
            send_telegram(msg)

        exits.append(event)

    # Save updated positions
    if exits and not dry_run:
        with open(positions_path, 'w') as f:
            json.dump(positions, f, indent=4)
        print(f'[MONITOR] {len(exits)} position(s) processed, positions.json updated')

    return exits


def main():
    parser = argparse.ArgumentParser(description='Exit monitor for open positions')
    parser.add_argument('--dry-run', action='store_true',
                        help='Check exit conditions but do not execute sells')
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    positions_path = str(base_dir / 'data' / 'positions.json')
    log_path = str(base_dir / 'data' / 'trade_log.jsonl')

    print(f'[MONITOR] Exit monitor starting (dry_run={args.dry_run})')
    exits = check_exits(positions_path, log_path, dry_run=args.dry_run)
    print(f'[MONITOR] Done. {len(exits)} exit(s) triggered.')


if __name__ == '__main__':
    main()