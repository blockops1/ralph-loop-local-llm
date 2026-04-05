import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from support_resistance import get_stop_loss
from range_scanner import MIN_POOL_LIQUIDITY_USD

RANGE_SCORE_MIN = 50
ENTRY_BUFFER_PCT = 4.0  # widened from 2.0% — catches tokens hovering near support across 30-min cycles
SCORE_THRESHOLD = 60
MIN_RANGE_WIDTH_PCT = 15.0  # range must be at least 15% wide (support→resistance) to cover fees + slippage

WETH_ADDRESS = '0x4200000000000000000000000000000000000006'


def _update_slipstream_routing(address: str, pool: dict):
    """
    DEPRECATED 2026-03-22: Slipstream routing is now statically configured in
    range_execute.SLIPSTREAM_TOKENS with on-chain verified tick spacings.
    This function is a no-op and will be removed in a future cleanup.
    """
    pass


def filter_signals(scan_results: list, open_positions: list) -> list:
    """
    Filter scanner output into BUY/SELL signals.

    Args:
        scan_results: list of dicts from range_scanner.scan_all()
        open_positions: list of dicts, each has 'symbol' and 'address' keys

    Returns:
        list of signal dicts
    """
    signals = []

    open_symbols = set(p['symbol'] for p in open_positions)
    open_addresses = set(p['address'].lower() for p in open_positions)

    for result in scan_results:
        if result.get('error') is not None:
            continue
        if result.get('range_score', 0) < RANGE_SCORE_MIN:
            continue

        # Hard disqualifier: range too narrow to trade profitably after fees + slippage
        sr = result.get('sr') or {}
        sup = sr.get('support')
        res = sr.get('resistance')
        if sup and res and sup > 0:
            range_width_pct = (res - sup) / sup * 100
            if range_width_pct < MIN_RANGE_WIDTH_PCT:
                print(f"[FILTER] SKIP {result.get('symbol','?')}: range too narrow "
                      f"({range_width_pct:.1f}% < {MIN_RANGE_WIDTH_PCT}% min)")
                continue

        # Hard disqualifier: pool reserve below minimum
        pool = result.get('pool', {})
        reserve_usd = pool.get('reserve_usd', 0)
        if reserve_usd < MIN_POOL_LIQUIDITY_USD:
            print(f"[FILTER] {result.get('symbol')}: disqualified — "
                  f"pool reserve ${reserve_usd:,.0f} < ${MIN_POOL_LIQUIDITY_USD:,}")
            continue

        # Auto-register Slipstream routing if needed
        _update_slipstream_routing(result.get('address', ''), pool)

        sr = result.get('sr')
        if sr is None:
            continue

        symbol = result['symbol']
        address = result['address']
        price_usd = result['price_usd']
        range_score = result['range_score']

        # BUY signal conditions
        if (sr['near_support'] is True and
            price_usd <= sr['entry_price'] * (1 + ENTRY_BUFFER_PCT / 100) and
            symbol not in open_symbols and
            address.lower() not in open_addresses and
            range_score >= SCORE_THRESHOLD):
            signals.append({
                'action': 'BUY',
                'symbol': symbol,
                'address': address,
                'price_usd': price_usd,
                'score': range_score,
                'support': sr['support'],
                'resistance': sr['resistance'],
                'entry_price': sr['entry_price'],
                'exit_price': sr['exit_price'],
                'stop_loss': get_stop_loss(sr),
            })
            continue

        # SELL signal conditions for open positions
        if symbol in open_symbols:
            stop_loss = get_stop_loss(sr)
            if sr['near_resistance'] is True:
                signals.append({
                    'action': 'SELL',
                    'symbol': symbol,
                    'address': address,
                    'price_usd': price_usd,
                    'reason': 'target',
                    'stop_loss': stop_loss,
                    'exit_price': sr['exit_price'],
                })
            elif price_usd <= stop_loss:
                signals.append({
                    'action': 'SELL',
                    'symbol': symbol,
                    'address': address,
                    'price_usd': price_usd,
                    'reason': 'stop',
                    'stop_loss': stop_loss,
                    'exit_price': sr['exit_price'],
                })

    return signals
