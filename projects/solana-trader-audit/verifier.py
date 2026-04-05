"""verifier.py - verify on-chain balances match positions.json"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).parent))
from positions import load_positions, save_positions
from execute import get_token_balance

WALLET = os.environ.get('SOLANA_WALLET_ADDRESS', '')
VERIFY_LOG = Path(__file__).parent.parent / 'data' / 'verify_log.jsonl'

def verify_positions():
    positions = load_positions()
    changed = False
    for pos in positions:
        if pos.get('status') not in ('open', 'partial'):
            continue
        symbol = pos['symbol']
        address = pos['address']
        recorded = pos.get('token_balance', 0)
        actual = get_token_balance(WALLET, address) if WALLET else 0
        discrepancy = abs(actual - recorded) / max(recorded, 0.0001)
        entry = {'ts': datetime.now(timezone.utc).isoformat(), 'symbol': symbol,
                 'recorded': recorded, 'actual': actual, 'discrepancy_pct': round(discrepancy*100,2)}
        if discrepancy > 0.05:
            print(f'[VERIFIER] {symbol}: recorded={recorded:.4f} actual={actual:.4f} ({discrepancy*100:.1f}% off)')
            pos['token_balance'] = actual
            changed = True
            entry['action'] = 'corrected'
        else:
            entry['action'] = 'ok'
        VERIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(VERIFY_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    if changed:
        save_positions(positions)
        print('[VERIFIER] Positions corrected')

if __name__ == '__main__':
    verify_positions()
