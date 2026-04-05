"""
range_scanner.py - Fetches OHLCV + price data for range-trader token list.

Data sources (all free, no API key required):
  - OHLCV: GeckoTerminal pool candles, disk-cached once per UTC day
  - Price: GeckoTerminal token info, in-process cache per run
  - Pool:  GeckoTerminal token pools (pool address, reserve_usd, dex_id)

Nansen: NOT used. Zero credits consumed.

Token list is hard-coded (conviction tokens, not discovery).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'

# Re-use the same GT OHLCV cache as base-trader if both run on the same machine.
# Range-trader uses its own key namespace inside the shared cache file.
GT_OHLCV_CACHE_FILE = DATA_DIR / 'gt_ohlcv_cache.json'

# Shared price cache (disk) - prevents 429s when verifier + scanner both run
GT_PRICE_CACHE_FILE = DATA_DIR / 'gt_price_cache.json'
GT_PRICE_CACHE_TTL = 300  # 5 minutes

TOKENS = {
    'WETH':  '0x4200000000000000000000000000000000000006',
    'AAVE':  '0x63706e401c06ac8513145b7687a14804d17f814b',
    'W':     '0xb0ffa8000886e57f86dd5264b9582b2ad87b2b91',
    'ZEN':   '0xf43eb8de897fbc7f2502483b2bef7bb9ea179229',
    'AERO':  '0x940181a94a35a4569e4529a3cdfb74e38fd98631',
    'cbBTC': '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf',
    'VVV':   '0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf',
    'BRETT': '0x532f27101965dd16442e59d40670faf5ebb142e4',
    'ZRO':   '0x6985884c4392d348587b19cb9eaaf157f13271cd',
}

CHAIN = 'base'
OHLCV_DAYS = 60  # request 60 days of daily candles

# GeckoTerminal rate limit: 10 calls/min free. 12s gap = 5/min (conservative).
_GT_MIN_INTERVAL = 12.0
_gt_last_call: float = 0.0


def _load_env():
    env_path = Path.home() / '.hermes' / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# GT throttle helper
# ---------------------------------------------------------------------------

def _gt_throttle():
    import time
    global _gt_last_call
    elapsed = time.time() - _gt_last_call
    if elapsed < _GT_MIN_INTERVAL:
        time.sleep(_GT_MIN_INTERVAL - elapsed)
    _gt_last_call = time.time()


def _gt_get(url: str, params: dict = None) -> dict:
    _gt_throttle()
    r = requests.get(url, params=params,
                     headers={'Accept': 'application/json;version=20230302'},
                     timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Pool lookup (GeckoTerminal) - cached per process
# ---------------------------------------------------------------------------

MIN_POOL_LIQUIDITY_USD = 25_000

_pool_cache: dict = {}


def get_top_pool(token_address: str, symbol: str) -> dict:
    """
    Query GeckoTerminal for the highest-liquidity pool for this token on Base.
    Returns dict: pool_address, dex_id, reserve_usd, tick_spacing, pool_name.
    Returns {} on error.  Cached for the process lifetime.
    """
    addr_lower = token_address.lower()
    if addr_lower in _pool_cache:
        return _pool_cache[addr_lower]

    result = {}
    try:
        data = _gt_get(
            f'https://api.geckoterminal.com/api/v2/networks/{CHAIN}/tokens/{addr_lower}/pools',
            params={'page': 1}
        )
        pools = data.get('data', [])
        if not pools:
            print(f'[POOL] {symbol}: no pools found on GeckoTerminal')
            _pool_cache[addr_lower] = result
            return result

        top = pools[0]
        attrs = top.get('attributes', {})
        rel = top.get('relationships', {})
        dex_id = rel.get('dex', {}).get('data', {}).get('id', '')
        reserve_usd = float(attrs.get('reserve_in_usd', 0) or 0)
        pool_address = attrs.get('address', '').lower()
        pool_name = attrs.get('name', '')

        # Detect tick_spacing for Slipstream CL pools
        tick_spacing = None
        if 'slipstream' in dex_id.lower():
            FEE_TO_TICK = {'0.01%': 1, '0.05%': 10, '0.15%': 100, '0.30%': 100,
                           '1.00%': 200, '2.00%': 200}
            for fee_str, ts in FEE_TO_TICK.items():
                if fee_str in pool_name:
                    tick_spacing = ts
                    break

        result = {
            'pool_address': pool_address,
            'dex_id': dex_id,
            'reserve_usd': reserve_usd,
            'tick_spacing': tick_spacing,
            'pool_name': pool_name,
        }
        print(f'[POOL] {symbol}: {dex_id} reserve=${reserve_usd:,.0f} pool={pool_address[:10]}...')
    except Exception as e:
        print(f'[POOL] {symbol}: GeckoTerminal error {e}')

    _pool_cache[addr_lower] = result
    return result


# ---------------------------------------------------------------------------
# OHLCV via GeckoTerminal (disk-cached once per UTC day)
# ---------------------------------------------------------------------------

def _ohlcv_cache_load() -> dict:
    try:
        return json.loads(GT_OHLCV_CACHE_FILE.read_text())
    except Exception:
        return {}


def _ohlcv_cache_save(cache: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GT_OHLCV_CACHE_FILE.write_text(json.dumps(cache))


def _ohlcv_entry_is_fresh(entry: dict) -> bool:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return entry.get('cached_date', '') == today


def _fetch_gt_ohlcv(pool_address: str, symbol: str) -> list:
    """Fetch daily OHLCV candles from GeckoTerminal for a pool."""
    try:
        data = _gt_get(
            f'https://api.geckoterminal.com/api/v2/networks/{CHAIN}/pools/{pool_address}/ohlcv/day',
            params={'limit': OHLCV_DAYS, 'currency': 'usd'}
        )
        raw = data.get('data', {}).get('attributes', {}).get('ohlcv_list', [])
        candles = []
        for item in raw:
            if len(item) < 6:
                continue
            ts, o, h, lo, c, vol = item
            candles.append({
                'timestamp': int(ts),
                'open':   float(o  or 0),
                'high':   float(h  or 0),
                'low':    float(lo or 0),
                'close':  float(c  or 0),
                'volume': float(vol or 0),
            })
        candles.sort(key=lambda x: x['timestamp'])
        print(f'[OHLCV] {symbol}: {len(candles)} candles from GeckoTerminal')
        return candles
    except Exception as e:
        print(f'[OHLCV] {symbol}: fetch error {e}')
        return []


def get_ohlcv(symbol: str, pool_address: str = '') -> list:
    """
    Return daily OHLCV candles for symbol. Disk-cached once per UTC day.
    pool_address required for fresh fetch; falls back to cache on 429/error.
    """
    cache = _ohlcv_cache_load()
    cache_key = f'range_{symbol}_usd'
    entry = cache.get(cache_key, {})

    if _ohlcv_entry_is_fresh(entry):
        return entry.get('candles', [])

    if not pool_address:
        # Can't fetch without pool address -- return stale cache if available
        candles = entry.get('candles', [])
        if candles:
            print(f'[OHLCV] {symbol}: using stale cache (no pool_address)')
        return candles

    candles = _fetch_gt_ohlcv(pool_address, symbol)
    if candles:
        cache[cache_key] = {
            'candles': candles,
            'cached_date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        }
        _ohlcv_cache_save(cache)

    return candles


# ---------------------------------------------------------------------------
# Price via GeckoTerminal (in-process cache, one call per token per run)
# ---------------------------------------------------------------------------

_price_cache: dict = {}


def _load_price_disk_cache() -> dict:
    try:
        return json.load(open(GT_PRICE_CACHE_FILE))
    except Exception:
        return {}


def _save_price_disk_cache(cache: dict) -> None:
    try:
        json.dump(cache, open(GT_PRICE_CACHE_FILE, 'w'))
    except Exception:
        pass


def get_price(token_address: str) -> float:
    """
    Return current price_usd for token from GeckoTerminal.
    Uses disk cache (5 min TTL) shared across all processes to prevent 429s.
    Returns 0.0 on error.
    """
    import time as _time
    addr_lower = token_address.lower()

    # In-process cache first (fastest)
    if addr_lower in _price_cache:
        return _price_cache[addr_lower]

    # Disk cache (shared across verifier, scanner, summary)
    now_ts = _time.time()
    disk_cache = _load_price_disk_cache()
    cache_key = f'{CHAIN}:{addr_lower}'
    if cache_key in disk_cache:
        entry = disk_cache[cache_key]
        if now_ts - entry.get('cached_at', 0) < GT_PRICE_CACHE_TTL:
            price = entry.get('data', {}).get('price_usd', 0.0)
            _price_cache[addr_lower] = price
            print(f'[PRICE] {addr_lower[:10]}...: ${price:.6f} (disk cache)')
            return price

    # Primary: DexScreener (no key, no rate limit)
    try:
        import requests as _req
        r = _req.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{addr_lower}',
            headers={'Accept': 'application/json'}, timeout=10)
        r.raise_for_status()
        pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == CHAIN]
        if pairs:
            best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
            price = float(best.get('priceUsd', 0) or 0)
            if price > 0:
                _price_cache[addr_lower] = price
                disk_cache[cache_key] = {'cached_at': now_ts, 'data': {'price_usd': price}}
                _save_price_disk_cache(disk_cache)
                print(f'[PRICE] {addr_lower[:10]}...: ${price:.6f} (dexscreener)')
                return price
    except Exception as e:
        print(f'[PRICE] {addr_lower[:10]}...: DexScreener error {e}')

    # Fallback 1: GeckoTerminal
    try:
        data = _gt_get(
            f'https://api.geckoterminal.com/api/v2/networks/{CHAIN}/tokens/{addr_lower}',
        )
        attrs = data.get('data', {}).get('attributes', {})
        price = float(attrs.get('price_usd', 0) or 0)
        if price > 0:
            _price_cache[addr_lower] = price
            disk_cache[cache_key] = {'cached_at': now_ts, 'data': {'price_usd': price}}
            _save_price_disk_cache(disk_cache)
            print(f'[PRICE] {addr_lower[:10]}...: ${price:.6f} (geckoterminal)')
            return price
    except Exception as e:
        print(f'[PRICE] {addr_lower[:10]}...: GT error {e}')

    # Fallback 2: stale disk cache
    if cache_key in disk_cache:
        price = disk_cache[cache_key].get('data', {}).get('price_usd', 0.0)
        age_m = (now_ts - disk_cache[cache_key].get('cached_at', 0)) / 60
        print(f'[PRICE] {addr_lower[:10]}...: ${price:.6f} (stale cache, {age_m:.0f}m old)')
        _price_cache[addr_lower] = price
        return price

    _price_cache[addr_lower] = 0.0
    return 0.0


def get_price_eth(token_address: str) -> float:
    """
    Return current price in ETH for token.
    Primary: DexScreener priceNative from highest-liquidity Base pair.
    Fallback: GeckoTerminal price_usd / ETH price_usd (from DexScreener WETH lookup).
    Returns 0.0 on error.
    """
    import time as _time
    addr_lower = token_address.lower()

    # In-process cache first
    if addr_lower in _price_cache:
        # Reuse USD price if available and convert
        usd_price = _price_cache.get(addr_lower, 0.0)
        if usd_price > 0:
            try:
                eth_price_usd = _get_eth_price_usd()
                if eth_price_usd > 0:
                    return usd_price / eth_price_usd
            except Exception:
                pass

    # Primary: DexScreener priceNative
    try:
        import requests as _req
        r = _req.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{addr_lower}',
            headers={'Accept': 'application/json'}, timeout=10)
        r.raise_for_status()
        pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == CHAIN]
        if pairs:
            best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
            price_native = float(best.get('priceNative', 0) or 0)
            if price_native > 0:
                print(f'[PRICE_ETH] {addr_lower[:10]}...: {price_native:.6f} ETH (dexscreener)')
                return price_native
    except Exception as e:
        print(f'[PRICE_ETH] {addr_lower[:10]}...: DexScreener error {e}')

    # Fallback: GeckoTerminal price_usd / ETH price_usd
    try:
        data = _gt_get(
            f'https://api.geckoterminal.com/api/v2/networks/{CHAIN}/tokens/{addr_lower}',
        )
        attrs = data.get('data', {}).get('attributes', {})
        price_usd = float(attrs.get('price_usd', 0) or 0)
        if price_usd > 0:
            eth_price_usd = _get_eth_price_usd()
            if eth_price_usd > 0:
                price_eth = price_usd / eth_price_usd
                print(f'[PRICE_ETH] {addr_lower[:10]}...: {price_eth:.6f} ETH (geckoterminal)')
                return price_eth
    except Exception as e:
        print(f'[PRICE_ETH] {addr_lower[:10]}...: GT error {e}')

    # Fallback 2: stale disk cache (USD) converted
    now_ts = _time.time()
    disk_cache = _load_price_disk_cache()
    cache_key = f'{CHAIN}:{addr_lower}'
    if cache_key in disk_cache:
        entry = disk_cache[cache_key]
        if now_ts - entry.get('cached_at', 0) < GT_PRICE_CACHE_TTL:
            price_usd = entry.get('data', {}).get('price_usd', 0.0)
            if price_usd > 0:
                try:
                    eth_price_usd = _get_eth_price_usd()
                    if eth_price_usd > 0:
                        price_eth = price_usd / eth_price_usd
                        print(f'[PRICE_ETH] {addr_lower[:10]}...: {price_eth:.6f} ETH (stale cache)')
                        return price_eth
                except Exception:
                    pass

    return 0.0


def _get_eth_price_usd() -> float:
    """
    Fetch ETH price in USD from DexScreener (WETH on Base).
    Returns 0.0 on error.
    """
    weth_address = '0x4200000000000000000000000000000000000006'
    try:
        import requests as _req
        r = _req.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{weth_address}',
            headers={'Accept': 'application/json'}, timeout=10)
        r.raise_for_status()
        pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == CHAIN]
        if pairs:
            best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
            price = float(best.get('priceUsd', 0) or 0)
            if price > 0:
                return price
    except Exception as e:
        print(f'[ETH_PRICE_USD] DexScreener error {e}')
    return 0.0


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan_all() -> list:
    """
    Scan all tokens. Returns list of scan result dicts, one per token.

    Each result:
      {symbol, address, candles_count, price_usd, price_eth, sr (dict or None),
       range_score (int), pool (dict), error (str or None)}
    """
    from support_resistance import calculate_sr_with_fallback, range_quality_score

    results = []
    for symbol, address in TOKENS.items():
        result = {
            'symbol': symbol,
            'address': address,
            'candles_count': 0,
            'price_usd': 0.0,
            'price_eth': 0.0,
            'sr': None,
            'range_score': 0,
            'pool': {},
            'error': None,
        }
        try:
            # Pool lookup first (also gives us pool_address for OHLCV)
            pool = get_top_pool(address, symbol)
            result['pool'] = pool
            reserve = pool.get('reserve_usd', 0)
            if reserve < MIN_POOL_LIQUIDITY_USD:
                result['error'] = (f'pool liquidity too low: '
                                   f'${reserve:,.0f} < ${MIN_POOL_LIQUIDITY_USD:,}')
                results.append(result)
                continue

            pool_address = pool.get('pool_address', '')
            candles = get_ohlcv(symbol, pool_address=pool_address)
            result['candles_count'] = len(candles)

            if len(candles) < 15:
                result['error'] = f'insufficient candles: {len(candles)}'
                results.append(result)
                continue

            price = get_price(address)
            # Fallback to last OHLCV close if GT doesn't have this token
            if price == 0.0 and candles:
                price = candles[-1]['close']
            result['price_usd'] = price

            price_eth = get_price_eth(address)
            # Fallback to USD price / ETH price if ETH price fetch fails
            if price_eth == 0.0 and result['price_usd'] > 0:
                try:
                    eth_price_usd = _get_eth_price_usd()
                    if eth_price_usd > 0:
                        price_eth = result['price_usd'] / eth_price_usd
                except Exception:
                    pass
            result['price_eth'] = price_eth

            sr = calculate_sr_with_fallback(candles)
            if sr is None:
                result['error'] = 'no S/R found'
                results.append(result)
                continue

            score = range_quality_score(candles, sr)
            result['sr'] = sr
            result['range_score'] = score

        except Exception as e:
            result['error'] = str(e)

        results.append(result)

    return results


if __name__ == '__main__':
    _load_env()
    results = scan_all()
    for r in results:
        sr = r['sr']
        if sr:
            print(f"{r['symbol']:6} | price_eth={r['price_eth']:.6f} | "
                  f"S={sr['support']:.6f} R={sr['resistance']:.6f} | "
                  f"score={r['range_score']} | "
                  f"near_support={sr['near_support']} near_res={sr['near_resistance']}")
        else:
            print(f"{r['symbol']:6} | error={r['error']}")
