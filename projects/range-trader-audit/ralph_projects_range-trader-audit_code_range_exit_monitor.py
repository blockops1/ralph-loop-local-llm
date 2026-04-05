import sys, os
from pathlib import Path
from datetime import datetime, timezone
import json
sys.path.insert(0, str(Path(__file__).parent))

# Load .env
_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from range_positions import load_positions, remove_position, save_positions
from range_execute import sell_token
from range_scanner import get_price, get_ohlcv, _load_env as scanner_load_env, get_top_pool
from support_resistance import calculate_sr, get_stop_loss, get_exit_price
import requests

TRADE_LOG = Path(__file__).parent.parent / 'data' / 'range_trade_log.jsonl'

# Time limit constants
WARN_HOURS = 336     # 14 days — warn but don't exit
FORCE_EXIT_HOURS = 720  # 30 days — force exit

# Near-stop alert: only fire once per position per session
# _near_stop_alerted removed 2026-03-23 — near-stop alerts disabled


def send_telegram(message: str):
    """Send a Telegram message via bot API to the trading group."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    group_id = '-5264050975'  # Trading group
    if not token or not group_id:
        print(f'[EXIT-MONITOR] Telegram not configured, skipping: {message[:80]}')
        return
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        requests.post(url, json={'chat_id': group_id, 'text': message}, timeout=10)
        print('[EXIT-MONITOR] Telegram alert sent')
    except Exception as e:
        print(f'[EXIT-MONITOR] Telegram error: {e}')


def check_positions():
    positions = load_positions()
    if not positions:
        print('[EXIT-MONITOR] No open positions')
        return

    positions_changed = False

    for pos in positions:
        symbol = pos['symbol']
        address = pos['address']

        # --- Bug 1 fix: use USD price, not ETH price ---
        # entry_price, stop_loss, exit_price are all stored in USD
        price = get_price(address)
        if price == 0.0:
            candles = get_ohlcv(symbol)
            if candles:
                price = candles[-1]['close']
        if price == 0.0:
            print(f'[EXIT-MONITOR] {symbol}: cannot get price, skipping')
            continue

        stop_loss = pos['stop_loss']
        exit_price = pos['exit_price']
        entry_price = pos['entry_price']
        entry_usdc = pos.get('entry_usdc', 0)
        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        pnl_usd = entry_usdc * pnl_pct / 100 if entry_usdc else 0.0

        print(f'[EXIT-MONITOR] {symbol}: price=${price:.4f} entry=${entry_price:.4f} '
              f'stop=${stop_loss:.4f} target=${exit_price:.4f} PnL={pnl_pct:.1f}% (${pnl_usd:.2f})')

        # --- Bug 5 fix: dynamic S/R refresh with pool_address ---
        candles = get_ohlcv(symbol)
        if not candles:
            # Try fetching fresh with pool_address
            pool = get_top_pool(address, symbol)
            pool_address = pool.get('pool_address', '')
            if pool_address:
                candles = get_ohlcv(symbol, pool_address=pool_address)

        if candles:
            new_sr = calculate_sr(candles)
            if new_sr:
                old_stop = pos['stop_loss']
                old_target = pos['exit_price']
                new_stop = get_stop_loss(new_sr)
                new_exit = get_exit_price(new_sr)

                # --- Bug 2 fix: ratchet UP only (protect gains, never loosen stop) ---
                if new_stop is not None and new_stop > old_stop:
                    pos['stop_loss'] = new_stop
                    positions_changed = True

                # Widen target only if new resistance is higher
                if new_exit is not None and new_exit > old_target:
                    pos['exit_price'] = new_exit
                    positions_changed = True

                if pos['stop_loss'] != old_stop or pos['exit_price'] != old_target:
                    print(f'[EXIT-MONITOR] {symbol}: S/R refreshed. '
                          f'Stop ${old_stop:.4f}→${pos["stop_loss"]:.4f}, '
                          f'Target ${old_target:.4f}→${pos["exit_price"]:.4f}')

        # Re-read updated values after potential ratchet
        stop_loss = pos['stop_loss']
        exit_price = pos['exit_price']

        reason = None
        if price <= stop_loss:
            reason = 'stop'
        elif price >= exit_price:
            reason = 'target'

        # Time-based checks
        entry_ts_str = pos.get('entry_ts')
        hold_hours = None
        if entry_ts_str:
            try:
                entry_ts = datetime.fromisoformat(entry_ts_str)
                hold_hours = (datetime.now(timezone.utc) - entry_ts).total_seconds() / 3600
            except (ValueError, TypeError):
                hold_hours = None

        if hold_hours is not None:
            if hold_hours >= FORCE_EXIT_HOURS:
                reason = 'time_limit'
            elif hold_hours >= WARN_HOURS and not pos.get('warned_stale'):
                days = hold_hours / 24
                send_telegram(f'Range-Trader WARNING: {symbol} open for {days:.1f} days — consider reviewing')
                pos['warned_stale'] = True
                positions_changed = True

        if reason:
            print(f'[EXIT-MONITOR] {symbol}: EXIT signal ({reason})')
            result = sell_token(address)
            if result['status'] == 'ok':
                hold_hours_calc = round(hold_hours, 1) if hold_hours is not None else 0.0
                trade_record = {
                    'action': 'SELL',
                    'symbol': symbol,
                    'address': address,
                    'entry_price': entry_price,
                    'exit_price_actual': price,
                    'entry_usdc': entry_usdc,
                    'pnl_pct': round(pnl_pct, 2),
                    'pnl_usd': round(pnl_usd, 2),
                    'reason': reason,
                    'hold_hours': hold_hours_calc,
                    'tx_hash': result.get('tx_hash', ''),
                    'tokens_sold': pos.get('tokens_held', 0),
                    'ts': datetime.now(timezone.utc).isoformat(),
                }
                with open(TRADE_LOG, 'a') as f:
                    f.write(json.dumps(trade_record) + '\n')

                remove_position(symbol)
                msg = (f'Range-Trader EXIT: {symbol} | reason={reason} | '
                       f'entry=${entry_price:.4f} | exit=${price:.4f} | '
                       f'PnL={pnl_pct:.1f}% (${pnl_usd:.2f}) | hold={hold_hours_calc}h')
                send_telegram(msg)
                print(f'[EXIT-MONITOR] {symbol}: sold OK, position removed')
            else:
                msg = f'Range-Trader EXIT FAILED: {symbol} | reason={reason} | error={result["error"]}'
                send_telegram(msg)
                print(f'[EXIT-MONITOR] {symbol}: sell failed: {result["error"]}')

        else:
            # Near-stop alert removed per user request 2026-03-23 — exit monitor handles stop execution
            pass

    # Persist any ratchet/stale-warning changes
    if positions_changed:
        save_positions(positions)


if __name__ == '__main__':
    scanner_load_env()
    check_positions()
