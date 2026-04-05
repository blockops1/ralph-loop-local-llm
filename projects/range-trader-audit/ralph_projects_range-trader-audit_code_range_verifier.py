"""
range_verifier.py - Onchain position validator for Range Trader

Validates that logged positions match actual token balances on Base.
For each open position in range_positions.json:
  1. Query on-chain token balance via web3 (same as reconcile.py)
  2. Get current price from GeckoTerminal (import get_price from range_scanner)
  3. Compute current_usd = token_balance * price_usd
  4. Compare with entry_usdc from position, compute discrepancy_pct
  5. Log structured JSON to data/range_verifier_log.jsonl
  6. On FAIL: send Telegram alert via TELEGRAM_ALLOWED_USERS

Usage:
  python3 scripts/range_verifier.py [--dry-run]
  echo '[. ..]' | python3 scripts/range_verifier.py --dry-run

Exit codes:
  0 = All positions verified (or no positions)
  1 = One or more positions failed verification
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime
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

from web3 import Web3
from eth_account import Account


def get_web3():
    """Get Web3 instance connected to Base mainnet."""
    BASE_RPC = 'https://mainnet.base.org'
    w3 = Web3(Web3.HTTPProvider(BASE_RPC, request_kwargs={'timeout': 30}))
    if not w3.is_connected():
        raise RuntimeError('Cannot connect to Base RPC')
    return w3


def get_token_balance(w3: Web3, token_address: str, wallet_address: str) -> float:
    """Get token balance for wallet_address, return as float."""
    ERC20_ABI = [
        {'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
         'inputs': [{'name': 'account', 'type': 'address'}],
         'outputs': [{'name': '', 'type': 'uint256'}]},
        {'name': 'decimals', 'type': 'function', 'stateMutability': 'view',
         'inputs': [], 'outputs': [{'name': '', 'type': 'uint8'}]}
    ]
    
    token_cs = Web3.to_checksum_address(token_address)
    token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
    
    balance_wei = token_contract.functions.balanceOf(wallet_address).call()
    decimals = token_contract.functions.decimals().call()
    
    return float(balance_wei) / (10 ** decimals)


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


def verify_position(position: dict, wallet_address: str) -> dict:
    """
    Verify a single position against on-chain balance and GeckoTerminal price.
    
    Returns verification result dict with:
      - ts
      - symbol
      - address
      - token_balance_onchain
      - price_usd
      - current_usd
      - entry_usdc
      - discrepancy_pct
      - stop_loss
      - status: 'pass' or 'fail'
      - error (if any)
    """
    symbol = position.get('symbol', 'UNKNOWN')
    address = position.get('address', '')
    entry_usdc = position.get('entry_usdc', 0.0)
    stop_loss = position.get('stop_loss', 0.0)
    
    result = {
        'ts': get_timestamp(),
        'symbol': symbol,
        'address': address,
        'token_balance_onchain': None,
        'price_usd': None,
        'current_usd': None,
        'entry_usdc': entry_usdc,
        'discrepancy_pct': None,
        'stop_loss': stop_loss,
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
    
    # Get current price from GeckoTerminal (import from range_scanner)
    from range_scanner import get_price
    price_usd = get_price(address)
    result['price_usd'] = price_usd
    
    if price_usd is None or price_usd == 0:
        result['error'] = 'Failed to get GeckoTerminal price'
        return result
    
    # Compute current USD value
    current_usd = token_balance * price_usd
    result['current_usd'] = current_usd
    
    # Compute discrepancy percentage
    if entry_usdc > 0:
        discrepancy_pct = abs(current_usd - entry_usdc) / entry_usdc * 100
    else:
        discrepancy_pct = 0.0 if current_usd == 0 else float('inf')
    result['discrepancy_pct'] = discrepancy_pct
    
    # Determine pass/fail
    # PASS if: discrepancy_pct < 25% AND token_balance > 0
    # FAIL if: discrepancy_pct >= 25%, OR token_balance == 0, OR price_usd <= stop_loss
    
    if token_balance == 0:
        result['status'] = 'fail'
        result['error'] = 'token_balance_zero'
        return result
    
    if discrepancy_pct >= 25:
        result['status'] = 'fail'
        result['error'] = 'discrepancy_too_high'
        return result
    
    # Check stop loss proximity
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
    parser = argparse.ArgumentParser(description='Verify onchain positions for Range Trader')
    parser.add_argument('--dry-run', action='store_true',
                        help='Read positions but skip API calls')
    parser.add_argument('--positions', default='data/range_positions.json',
                        help='Path to range_positions.json')
    parser.add_argument('--log', default='data/range_verifier_log.jsonl',
                        help='Path to verifier log (range_verifier_log.jsonl)')
    
    parsed = parser.parse_args(args)
    
    # Read wallet address from env (METAMASK_WALLET_ADDRESS)
    wallet_address = os.environ.get('METAMASK_WALLET_ADDRESS', '')
    if not wallet_address:
        print('[ERROR] METAMASK_WALLET_ADDRESS not set in .env')
        return 1
    
    # Read positions (from stdin or file)
    positions = read_positions(parsed.positions)
    
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
            'entry_usdc': 0,
            'discrepancy_pct': 0,
            'stop_loss': 0,
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
                'entry_usdc': position.get('entry_usdc', 0),
                'discrepancy_pct': None,
                'stop_loss': position.get('stop_loss', 0),
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
        
        # On FAIL: send Telegram alert
        if result['status'] == 'fail':
            error = result.get('error', 'unknown')
            discrepancy = result['discrepancy_pct']
            disc_str = f"{discrepancy:.1f}%" if discrepancy is not None else "N/A"
            alarm_msg = f"{symbol}: {error} (balance={result['token_balance_onchain']}, price={result['price_usd']}, discrepancy={disc_str})"
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
    
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
