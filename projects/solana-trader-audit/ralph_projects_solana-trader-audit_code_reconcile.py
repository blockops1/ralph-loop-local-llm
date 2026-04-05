"""reconcile.py - find and mark missing positions; enforce cooldowns"""
import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).parent))
from positions import load_positions, save_positions
from execute import get_token_balance
from decision import set_cooldown

WALLET = os.environ.get('SOLANA_WALLET_ADDRESS', '')

def reconcile():
    positions = load_positions()
    changed = False
    for pos in positions:
        if pos.get('status') not in ('open', 'partial'):
            continue
        symbol = pos['symbol']
        address = pos['address']
        actual = get_token_balance(WALLET, address) if WALLET else None
        if actual is not None and actual < 0.001:
            # On-chain balance gone - mark closed_missing
            print(f'[RECONCILE] {symbol}: on-chain balance zero - marking closed_missing')
            pos['status'] = 'closed_missing'
            pos['closed_at'] = datetime.now(timezone.utc).isoformat()
            set_cooldown(symbol)
            changed = True
    if changed:
        save_positions(positions)

if __name__ == '__main__':
    reconcile()
