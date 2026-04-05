#!/usr/bin/env python3
"""
range_reconcile.py - on-chain position reconciler for Range Trader

Reads data/range_positions.json, checks each open position's token balance
on Base mainnet via web3 balanceOf using METAMASK_WALLET_ADDRESS. If balance == 0,
marks the position as removed and logs the event to data/range_reconcile_log.jsonl.

Called by run_range_pipeline.py at startup before scanning.
Can also be run standalone: python3 scripts/range_reconcile.py

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

BASE_RPC = 'https://mainnet.base.org'

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


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(BASE_RPC, request_kwargs={'timeout': 30}))
    if not w3.is_connected():
        raise RuntimeError('Cannot connect to Base RPC')
    return w3


def get_token_balance(w3: Web3, token_address: str, wallet_address: str) -> float:
    """
    Returns token balance as a float in token units.
    Returns 0.0 on any error.
    """
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_BALANCE_ABI
        )
        decimals = contract.functions.decimals().call()
        raw = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet_address)
        ).call()
        return raw / (10 ** decimals)
    except Exception as e:
        print(f'[RECONCILE] balanceOf error for {token_address}: {e}')
        return 0.0


def reconcile(positions_path: str, log_path: str, wallet_address: str) -> list:
    """
    Reconcile range_positions.json against on-chain balances.
    Returns updated positions list.
    Writes reconciliation events to log_path (JSONL append).
    """
    try:
        with open(positions_path) as f:
            positions = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print('[RECONCILE] No range_positions.json or invalid JSON - nothing to reconcile')
        return []

    if not positions:
        print('[RECONCILE] No positions to reconcile')
        return positions

    w3 = get_web3()

    changed = False
    for pos in positions:
        # Positions without a "status" field are implicitly open
        if pos.get('status', 'open') != 'open':
            continue

        symbol = pos.get('symbol', '?')
        address = pos.get('address', '')
        if not address:
            continue

        balance = get_token_balance(w3, address, wallet_address)
        print(f'[RECONCILE] {symbol}: on-chain balance = {balance:.6f} tokens')

        if balance == 0.0:
            print(f'[RECONCILE] {symbol}: balance=0, marking removed')
            pos['status'] = 'removed'
            pos['closed_at'] = datetime.now(timezone.utc).isoformat()
            pos['close_reason'] = 'reconcile_zero_balance'
            event = {
                'ts': datetime.now(timezone.utc).isoformat(),
                'event': 'RECONCILE_REMOVED',
                'symbol': symbol,
                'address': address,
                'reason': 'on_chain_balance_zero'
            }
            with open(log_path, 'a') as f:
                f.write(json.dumps(event) + '\n')
            changed = True
        else:
            # Update stored token balance
            pos['tokens_held'] = balance
            changed = True

    if changed:
        with open(positions_path, 'w') as f:
            json.dump(positions, f, indent=4)
        print('[RECONCILE] range_positions.json updated')

    return positions


def main():
    wallet = os.environ.get('METAMASK_WALLET_ADDRESS', '')
    if not wallet:
        print('[RECONCILE] ERROR: METAMASK_WALLET_ADDRESS not set')
        sys.exit(1)

    base_dir = Path(__file__).parent.parent
    positions_path = str(base_dir / 'data' / 'range_positions.json')
    log_path = str(base_dir / 'data' / 'range_reconcile_log.jsonl')

    print(f'[RECONCILE] Wallet: {wallet}')
    print(f'[RECONCILE] Positions: {positions_path}')

    reconcile(positions_path, log_path, wallet)
    print('[RECONCILE] Done')


if __name__ == '__main__':
    main()
