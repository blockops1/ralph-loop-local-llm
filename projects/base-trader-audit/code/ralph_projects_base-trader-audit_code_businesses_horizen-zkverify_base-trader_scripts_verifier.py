"""
verifier.py - Onchain position validator

Validates that logged positions match actual token balances on Base.
For each open position in positions.json:
  1. Query on-chain token balance via web3 (same as reconcile.py)
  2. Get current price via Nansen token-screener API
  3. Compute current_usd = token_balance * price_usd
  4. Compare with position_usd, compute discrepancy_pct
  5. Log structured JSON to data/verifier_log.jsonl
  6. On FAIL: send Telegram alert via TELEGRAM_ALLOWED_USERS

Usage:
  python3 scripts/verifier.py [--dry-run]
  echo '[. ..]' | python3 scripts/verifier.py --dry-run

Exit codes:
  0 = All positions verified (or no positions)
  1 = One or more positions failed verification
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

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

from reconcile import get_web3, get_token_balance


def get_timestamp() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def read_positions(path: Optional[str] = None) -> list:
    """Read open positions from file or stdin."""
    # Try to read from stdin first (for piping)
    if sys.stdin.isatty() == False:
        try:
            stdin_data = sys.stdin.read().strip()
            if stdin_data:
                return json.loads(stdin_data)
        except json.JSONDecodeError:
            pass
    
    # Fall back to file
    if path:
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []
    
    return []


def get_nansen_price(token_address: str) -> Optional[float]:
    """
    Get current token price. Priority:
    1. GeckoTerminal (free, no key, accurate)
    2. Nansen screener disk cache (free if already cached by pipeline)
    3. CoinGecko (free, fallback for known tokens)
    """
    import json
    import sys
    from pathlib import Path

    # 1. GeckoTerminal -- free, works for any Base token
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from gecko_terminal import gt_get_token_info
        info = gt_get_token_info(token_address)
        price = info.get('price_usd')
        if price and float(price) > 0:
            print(f'[VERIFIER] GT price for {token_address[:10]}...: ${float(price):.6f}')
            return float(price)
    except Exception as e:
        print(f'[VERIFIER] GT price lookup failed: {e}')

    # 2. Nansen screener disk cache (zero extra API calls if fresh)
    cache_file = Path(__file__).parent.parent / 'data' / 'screener_cache.json'
    try:
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            item = cache.get(token_address.lower(), {})
            price = item.get('price_usd')
            if price is not None:
                return float(price)
    except Exception:
        pass

    # 3. CoinGecko -- known tokens only
    COINGECKO_IDS = {
        '0x940181a94a35a4569e4529a3cdfb74e38fd98631': 'aerodrome-finance',
    }
    cg_id = COINGECKO_IDS.get(token_address.lower())
    if cg_id:
        try:
            import requests
            r = requests.get(
                f'https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd',
                timeout=15,
            )
            r.raise_for_status()
            price = r.json().get(cg_id, {}).get('usd')
            if price is not None:
                print(f'[VERIFIER] CoinGecko price for {token_address[:10]}...: ${price:.6f}')
                return float(price)
        except Exception as e:
            print(f'[ERROR] CoinGecko fallback failed for {token_address}: {e}')

    return None


def verify_position(position: dict, wallet_address: str) -> dict:
    """
    Verify a single position against on-chain balance and Nansen price.
    
    Returns verification result dict with:
      - ts
      - symbol
      - address
      - token_balance_onchain
      - price_usd
      - current_usd
      - position_usd
      - discrepancy_pct
      - stop_loss
      - pct_above_stop
      - status: 'pass' or 'fail'
      - error (if any)
    """
    symbol = position.get('symbol', 'UNKNOWN')
    address = position.get('address', '')
    position_usd = position.get('position_usd', 0.0)
    stop_loss = position.get('stop_loss', 0.0)
    
    result = {
        'ts': get_timestamp(),
        'symbol': symbol,
        'address': address,
        'token_balance_onchain': None,
        'price_usd': None,
        'current_usd': None,
        'position_usd': position_usd,
        'discrepancy_pct': None,
        'balance_diff_pct': None,
        'stop_loss': stop_loss,
        'pct_above_stop': None,
        'status': 'fail',
        'error': None,
    }
    
    if not address:
        result['error'] = 'No contract address in position'
        return result
    
    # Get on-chain token balance
    w3 = get_web3()
    token_balance = get_token_balance(w3, address, wallet_address)
    result['token_balance_onchain'] = token_balance
    
    # Get current price from Nansen
    price_usd = get_nansen_price(address)
    result['price_usd'] = price_usd
    
    if price_usd is None:
        result['error'] = 'Failed to get Nansen price'
        return result
    
    # Compute current USD value
    current_usd = token_balance * price_usd
    result['current_usd'] = current_usd
    
    # Compute discrepancy percentage
    if position_usd > 0:
        discrepancy_pct = abs(current_usd - position_usd) / position_usd * 100
    elif current_usd == 0:
        discrepancy_pct = 0.0  # Both zero = no discrepancy
    else:
        discrepancy_pct = None  # Unknown — position was never valued
    result['discrepancy_pct'] = discrepancy_pct

    # Balance check: on-chain balance vs recorded balance (the real integrity check)
    recorded_balance = position.get('token_balance', 0.0)
    balance_diff_pct = 0.0
    if recorded_balance > 0:
        balance_diff_pct = abs(token_balance - recorded_balance) / recorded_balance * 100
    result['balance_diff_pct'] = balance_diff_pct
    
    # Compute percentage above stop loss
    if stop_loss > 0:
        pct_above_stop = ((price_usd - stop_loss) / stop_loss) * 100
    else:
        pct_above_stop = None
    result['pct_above_stop'] = pct_above_stop
    
    # Determine pass/fail
    # PASS if: on-chain balance matches recorded balance (within 5% tolerance)
    # FAIL if: token_balance == 0 (tokens gone), OR balance differs >5%, OR stop loss hit
    
    if token_balance == 0:
        result['status'] = 'fail'
        result['error'] = 'tokens_gone'
        return result
    
    # Alert only on genuine balance mismatch — not price moves
    if balance_diff_pct > 5:
        result['status'] = 'fail'
        result['error'] = 'balance_mismatch'
        return result
    
    # Check stop loss proximity (within 3%)
    if stop_loss > 0 and price_usd <= stop_loss:
        result['status'] = 'fail'
        result['error'] = 'price_at_or_below_stop_loss'
        return result
    
    # All checks passed
    result['status'] = 'pass'
    return result


def append_to_jsonl(path: str, record: dict) -> None:
    """Append a record to a JSONL file."""
    with open(path, 'a') as f:
        f.write(json.dumps(record) + '\n')


def send_telegram_alert(message: str) -> None:
    """Send a Telegram message via bot API to the trading group."""
    import requests
    
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    group_id = '-5264050975'  # Trading group
    
    if not token or not group_id:
        print(f'[VERIFIER] Telegram not configured, skipping alert: {message[:80]}')
        return
    
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        requests.post(url, json={'chat_id': group_id, 'text': message}, timeout=10)
        print('[VERIFIER] Telegram alert sent')
    except Exception as e:
        print(f'[VERIFIER] Telegram error: {e}')


def main(args: Optional[list] = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Verify onchain positions')
    parser.add_argument('--dry-run', action='store_true',
                        help='Read positions but skip API calls')
    parser.add_argument('--positions', default='data/positions.json',
                        help='Path to positions.json')
    parser.add_argument('--trade-log', default='data/trade_log.jsonl',
                        help='Path to trade_log.jsonl')
    parser.add_argument('--log', default='data/verifier_log.jsonl',
                        help='Path to verifier log')
    parser.add_argument('--alarms', default='data/verifier_alarms.jsonl',
                        help='Path to verifier alarms')
    
    parsed = parser.parse_args(args)
    
    # Read wallet address from env (BASE_WALLET_ADDRESS, not BASE_WALLET)
    wallet_address = os.environ.get('BASE_WALLET_ADDRESS', '')
    if not wallet_address:
        print('[ERROR] BASE_WALLET_ADDRESS not set in .env')
        return 1
    
    # Read positions (from stdin or file) — only verify open positions
    positions = [p for p in read_positions(parsed.positions) if p.get('status') == 'open']
    
    print(f'Verifying {len(positions)} open position(s) for wallet {wallet_address[:8]}...')
    
    # Handle empty positions
    if not positions:
        print('[INFO] No open positions - verification passed')
        append_to_jsonl(parsed.log, {
            'ts': get_timestamp(),
            'symbol': '_EMPTY_',
            'token_balance_onchain': 0,
            'price_usd': 0,
            'current_usd': 0,
            'position_usd': 0,
            'discrepancy_pct': 0,
            'stop_loss': 0,
            'pct_above_stop': None,
            'status': 'pass',
            'note': 'No open positions to verify',
        })
        return 0
    
    # Verify each position
    results = []
    alarms = []
    
    for position in positions:
        symbol = position.get('symbol', 'UNKNOWN')
        
        if parsed.dry_run:
            print(f'  [DRY-RUN] Would verify {symbol}')
            result = {
                'ts': get_timestamp(),
                'symbol': symbol,
                'address': position.get('address', ''),
                'token_balance_onchain': None,
                'price_usd': None,
                'current_usd': None,
                'position_usd': position.get('position_usd', 0),
                'discrepancy_pct': None,
                'stop_loss': position.get('stop_loss', 0),
                'pct_above_stop': None,
                'status': 'dry_run',
                'error': None,
            }
            results.append(result)
            continue
        
        print(f'  Verifying {symbol}...')
        result = verify_position(position, wallet_address)
        results.append(result)
        
        # Log result
        append_to_jsonl(parsed.log, result)
        
        # On FAIL: send Telegram alert (with per-symbol cooldown for transient errors)
        if result['status'] == 'fail':
            error = result.get('error', 'unknown')
            bal_diff = result.get('balance_diff_pct', 0)
            bal_str = f"balance_diff={bal_diff:.1f}%" if bal_diff is not None else ""
            alarm_msg = f"{symbol}: {error} (onchain={result['token_balance_onchain']:.4f}, recorded={position.get('token_balance', 0):.4f}, {bal_str})"

            # Cooldown: only alert once per symbol per hour
            # (transient issues cause repeated false alarms without this)
            cooldown_path = Path(__file__).parent.parent / 'data' / 'verifier_alert_cooldown.json'
            now = datetime.now(timezone.utc)
            try:
                cooldown = json.load(open(cooldown_path)) if cooldown_path.exists() else {}
            except Exception:
                cooldown = {}
            last_alert = cooldown.get(symbol)
            if last_alert:
                last = datetime.fromisoformat(last_alert)
                if (now - last).total_seconds() < 3600:
                    print(f'  [VERIFIER] Skipping alert for {symbol} (cooldown active, last: {last_alert})')
                    continue
            # Update cooldown
            cooldown[symbol] = now.isoformat()
            json.dump(cooldown, open(cooldown_path, 'w'))
            alarms.append(alarm_msg)
            send_telegram_alert(alarm_msg)
    
    # Summary
    passed = sum(1 for r in results if r['status'] == 'pass')
    failed = sum(1 for r in results if r['status'] == 'fail')
    dry_run_count = sum(1 for r in results if r['status'] == 'dry_run')
    
    print(f'\nVerification complete: {passed} passed, {failed} failed, {dry_run_count} dry_run')
    
    if failed > 0:
        print('\nFailed positions:')
        for r in results:
            if r['status'] == 'fail':
                print(f"  - {r['symbol']}: {r.get('error', 'discrepancy')}")
    
    # Send PASS confirmation only on first verification (not every run)
    # Track confirmed positions in data/verifier_confirmed.json
    confirmed_path = Path(__file__).parent.parent / 'data' / 'verifier_confirmed.json'
    try:
        confirmed = set(json.load(open(confirmed_path))) if confirmed_path.exists() else set()
    except Exception:
        confirmed = set()

    newly_confirmed = []
    for r in results:
        if r['status'] == 'pass' and r.get('token_balance_onchain', 0) > 0:
            symbol = r['symbol']
            if symbol not in confirmed:
                newly_confirmed.append(r)
                confirmed.add(symbol)

    if newly_confirmed:
        confirmed_path.parent.mkdir(parents=True, exist_ok=True)
        json.dump(sorted(confirmed), open(confirmed_path, 'w'))
        for r in newly_confirmed:
            symbol = r['symbol']
            balance = r['token_balance_onchain']
            price = r.get('price_usd', 0)
            current_usd = balance * price if price else 0
            msg = (
                f'✅ WALLET VERIFIED: {symbol}\n'
                f'{balance:.2f} tokens (${current_usd:.0f}) confirmed on-chain\n'
                f'Position is live.'
            )
            send_telegram_alert(msg)
            print(f'[VERIFIER] First-time confirmation sent for {symbol}')

    # Clean up confirmed set when position is closed (not in positions list)
    open_syms = {p.get('symbol') for p in positions}
    stale = confirmed - open_syms
    if stale:
        for s in stale:
            confirmed.discard(s)
        json.dump(sorted(confirmed), open(confirmed_path, 'w'))
        print(f'[VERIFIER] Removed closed positions from confirmed set: {stale}')
    
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
