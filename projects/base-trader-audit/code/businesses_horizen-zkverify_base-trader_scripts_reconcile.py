#!/usr/bin/env python3
"""
reconcile.py - on-chain position reconciler

Reads data/positions.json, checks each open position's token balance
on Base mainnet via web3 balanceOf. If balance == 0, marks the position
closed with status='closed_missing' and logs the event.

Called by run_pipeline.py at startup before any signal scoring.
Can also be run standalone: python3 scripts/reconcile.py

Exit codes:
  0 = all positions verified or reconciled
  1 = error (RPC failure, file error)
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

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
from execute import get_web3  # uses RPC fallback from execute.py

ERC20_BALANCE_ABI = [
    {
        'constant': True,
        'inputs': [{'name': '_owner', 'type': 'address'}],
        'name': 'balanceOf',
        'outputs': [{'name': 'balance', 'type': 'uint256'}],
        'type': 'function'
    },
    {
        'constant': True,
        'inputs': [],
        'name': 'decimals',
        'outputs': [{'name': '', 'type': 'uint8'}],
        'type': 'function'
    }
]


def get_token_balance(w3: Web3, token_address: str, wallet_address: str, retries: int = 3) -> float:
    """
    Returns token balance as a float in token units.
    Retries up to `retries` times on failure to handle transient RPC issues.
    Returns 0.0 if all attempts fail.
    """
    for attempt in range(retries):
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_BALANCE_ABI
            )
            decimals = contract.functions.decimals().call()
            raw = contract.functions.balanceOf(
                Web3.to_checksum_address(wallet_address)
            ).call()
            balance = raw / (10 ** decimals)
            # If balance is 0, retry once to confirm (avoid false zeros)
            if balance == 0.0 and attempt < retries - 1:
                import time
                time.sleep(0.5)
                continue
            return balance
        except Exception as e:
            if attempt == retries - 1:
                print(f'[RECONCILE] balanceOf error for {token_address}: {e}')
                return 0.0
            import time
            time.sleep(0.5)


def reconcile(positions_path: str, log_path: str, wallet_address: str) -> list:
    """
    Reconcile positions.json against on-chain balances.
    Returns updated positions list.
    Writes reconciliation events to log_path (JSONL append).
    """
    try:
        with open(positions_path) as f:
            positions = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print('[RECONCILE] No positions.json or invalid JSON - nothing to reconcile')
        return []

    if not positions:
        print('[RECONCILE] No positions to reconcile')
        return positions

    w3 = get_web3()

    # Check each open position against on-chain balance
    for pos in positions:
        if pos.get('status') not in ('open', 'partial_exit'):
            continue

        symbol = pos.get('symbol', '?')
        address = pos.get('address', '')
        if not address:
            continue

        balance = get_token_balance(w3, address, wallet_address)
        print(f'[RECONCILE] {symbol}: on-chain balance = {balance:.6f} tokens')

        if balance == 0.0:
            print(f'[RECONCILE] {symbol}: balance=0, marking closed_missing')
            pos['status'] = 'closed_missing'
            pos['closed_at'] = datetime.now(timezone.utc).isoformat()
            pos['close_reason'] = 'reconcile_zero_balance'
            event = {
                'ts': datetime.now(timezone.utc).isoformat(),
                'event': 'RECONCILE_CLOSED',
                'symbol': symbol,
                'address': address,
                'reason': 'on_chain_balance_zero'
            }
            with open(log_path, 'a') as f:
                f.write(json.dumps(event) + '\n')
        else:
            # Update recorded balance to match on-chain reality (corrects drift from partial exits)
            pos['token_balance'] = balance
            entry_price = pos.get('entry_price', 0)
            if entry_price and balance:
                pos['position_usd'] = balance * entry_price

    # Prune closed positions (keep only open/partial_exit) to prevent file bloat
    live = [p for p in positions if p.get('status') in ('open', 'partial_exit')]
    pruned = len(positions) - len(live)
    if pruned > 0:
        with open(positions_path, 'w') as f:
            json.dump(live, f, indent=2)
        print(f'[RECONCILE] Pruned {pruned} closed entries, kept {len(live)} live')

    return live


def main():
    wallet = os.environ.get('BASE_WALLET_ADDRESS', '')
    if not wallet:
        print('[RECONCILE] ERROR: BASE_WALLET_ADDRESS not set')
        sys.exit(1)

    base_dir = Path(__file__).parent.parent
    positions_path = str(base_dir / 'data' / 'positions.json')
    log_path = str(base_dir / 'data' / 'reconcile_log.jsonl')

    print(f'[RECONCILE] Wallet: {wallet}')
    print(f'[RECONCILE] Positions: {positions_path}')

    reconcile(positions_path, log_path, wallet)
    print('[RECONCILE] Done')


if __name__ == '__main__':
    main()
