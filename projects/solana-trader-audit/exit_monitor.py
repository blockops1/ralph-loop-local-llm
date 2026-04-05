import os, json, requests, time
from pathlib import Path
from datetime import datetime, timezone

def get_price_sol(token_address: str) -> float:
    """Fetch token price in SOL via DexScreener priceNative. Returns 0.0 on error."""
    try:
        r = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{token_address}',
            headers={'Accept': 'application/json'}, timeout=10)
        if r.status_code == 200:
            pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == 'solana']
            if pairs:
                best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
                native = float(best.get('priceNative', 0) or 0)
                if native > 0:
                    return native
    except Exception as e:
        print(f'[EXIT] price fetch error {token_address}: {e}')
    return 0.0

def check_exits(positions: list, config: dict, paper_trade: bool = True) -> list:
    """
    Check all open positions for exit conditions.
    Modifies positions in-place (updates tier1_hit, tier2_hit, peak_price_sol, status).
    Returns list of exit actions taken: [{'symbol', 'action', 'reason', 'pct_pnl', 'sell_fraction'}]
    """
    from execute import sell_token
    from decision import log_trade

    exits_taken = []
    stop_loss_pct = config.get('stop_loss_pct', 0.20)      # -20%
    tier1_pct = config.get('tier1_pct', 0.50)              # +50%
    tier2_pct = config.get('tier2_pct', 1.00)              # +100%
    trailing_pct = config.get('trailing_pct', 0.30)        # 30% from peak

    for pos in positions:
        if pos.get('status') != 'open' and pos.get('status') != 'partial':
            continue
        symbol = pos['symbol']
        entry = pos.get('entry_price_sol', 0)
        current = get_price_sol(pos['address'])
        if current <= 0 or entry <= 0:
            continue

        # Update peak
        peak = pos.get('peak_price_sol', entry)
        if current > peak:
            pos['peak_price_sol'] = current
            peak = current

        pnl_pct = (current - entry) / entry

        # Tier 1: +50% - sell 50%
        if pnl_pct >= tier1_pct and not pos.get('tier1_hit'):
            tokens = pos.get('token_balance', 0) * 0.50
            result = sell_token(pos['address'], symbol, tokens, paper_trade=paper_trade)
            if result.get('success'):
                pos['tier1_hit'] = True
                pos['token_balance'] = pos.get('token_balance', 0) * 0.50
                pos['stop_loss'] = entry   # move stop to breakeven
                pos['status'] = 'partial'
                log_trade({'ts': datetime.now(timezone.utc).isoformat(), 'symbol': symbol,
                            'action': 'SELL_TIER1', 'pnl_pct': pnl_pct, 'tx': result.get('tx')})
                exits_taken.append({'symbol': symbol, 'action': 'SELL_TIER1',
                                    'reason': f'+{pnl_pct*100:.1f}%', 'sell_fraction': 0.50})
            else:
                pos['status'] = 'exit_failed'

        # Tier 2: +100% - sell 50% of remaining (25% of original)
        elif pnl_pct >= tier2_pct and pos.get('tier1_hit') and not pos.get('tier2_hit'):
            tokens = pos.get('token_balance', 0) * 0.50
            result = sell_token(pos['address'], symbol, tokens, paper_trade=paper_trade)
            if result.get('success'):
                pos['tier2_hit'] = True
                pos['token_balance'] = pos.get('token_balance', 0) * 0.50
                log_trade({'ts': datetime.now(timezone.utc).isoformat(), 'symbol': symbol,
                            'action': 'SELL_TIER2', 'pnl_pct': pnl_pct, 'tx': result.get('tx')})
                exits_taken.append({'symbol': symbol, 'action': 'SELL_TIER2',
                                    'reason': f'+{pnl_pct*100:.1f}%', 'sell_fraction': 0.25})
            else:
                pos['status'] = 'exit_failed'

        # Trailing stop on remaining 25% (after tier2 hit)
        elif pos.get('tier2_hit') and current < peak * (1 - trailing_pct):
            tokens = pos.get('token_balance', 0)
            result = sell_token(pos['address'], symbol, tokens, paper_trade=paper_trade)
            if result.get('success'):
                pos['status'] = 'closed'
                log_trade({'ts': datetime.now(timezone.utc).isoformat(), 'symbol': symbol,
                            'action': 'SELL_TRAILING', 'pnl_pct': pnl_pct, 'tx': result.get('tx')})
                exits_taken.append({'symbol': symbol, 'action': 'SELL_TRAILING',
                                    'reason': f'trailing stop at {current:.8f}', 'sell_fraction': 1.0})
            else:
                pos['status'] = 'exit_failed'

        # Hard stop loss
        elif pnl_pct <= -stop_loss_pct:
            tokens = pos.get('token_balance', 0)
            result = sell_token(pos['address'], symbol, tokens, paper_trade=paper_trade)
            if result.get('success'):
                pos['status'] = 'closed'
                log_trade({'ts': datetime.now(timezone.utc).isoformat(), 'symbol': symbol,
                            'action': 'STOP_LOSS', 'pnl_pct': pnl_pct, 'tx': result.get('tx')})
                exits_taken.append({'symbol': symbol, 'action': 'STOP_LOSS',
                                    'reason': f'{pnl_pct*100:.1f}%', 'sell_fraction': 1.0})
            else:
                pos['status'] = 'exit_failed'

        # Breakeven stop (after tier1 hit)
        elif pos.get('tier1_hit') and current < pos.get('stop_loss', 0):
            tokens = pos.get('token_balance', 0)
            result = sell_token(pos['address'], symbol, tokens, paper_trade=paper_trade)
            if result.get('success'):
                pos['status'] = 'closed'
                log_trade({'ts': datetime.now(timezone.utc).isoformat(), 'symbol': symbol,
                            'action': 'BREAKEVEN_STOP', 'pnl_pct': pnl_pct, 'tx': result.get('tx')})
                exits_taken.append({'symbol': symbol, 'action': 'BREAKEVEN_STOP',
                                    'reason': f'below breakeven at {current:.8f}', 'sell_fraction': 1.0})
            else:
                pos['status'] = 'exit_failed'

    return exits_taken
