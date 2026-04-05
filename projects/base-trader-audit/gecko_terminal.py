"""
gecko_terminal.py - Free GeckoTerminal API wrapper for Base DEX data

Provides:
- gt_discover_tokens(): trending/high-volume tokens on Base (free)
- gt_get_ohlcv(): daily OHLCV candles via pool address (disk-cached daily)
- gt_get_token_info(): price, volume, liquidity for a token (free)

GeckoTerminal free tier: 10 calls/min. This module self-throttles with 7s
sleep between calls to stay comfortably under the limit.

No API key required.
"""

import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta


# Symbols to skip (stablecoins, wrapped assets)
_SKIP_SYMBOLS = {"USDC", "USDT", "DAI", "USDS", "WETH", "WBTC", "cbBTC", "cbETH"}

DATA_DIR = Path(__file__).parent.parent / 'data'
_GT_OHLCV_CACHE_FILE = DATA_DIR / 'gt_ohlcv_cache.json'

# Cache TTL
_OHLCV_TTL_SECONDS = 86400  # 24h -- daily candles don't change mid-day
_PRICE_CACHE_TTL = 300       # 5 min -- shared across all processes to avoid GT 429s
_PRICE_CACHE_FILE = DATA_DIR / 'gt_price_cache.json'

# Rate limiting state
_last_call_ts: float = 0.0
_MIN_INTERVAL = 12.0  # seconds between calls (5/min conservative, free limit is 10/min)


def _throttle() -> None:
    """Sleep if needed to stay under 10 calls/min."""
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call_ts = time.time()


def _gt_get(url: str, params: dict = None) -> dict:
    """Make a throttled GET to GeckoTerminal. Raises on non-200."""
    _throttle()
    headers = {'Accept': 'application/json;version=20230302'}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Token discovery
# ---------------------------------------------------------------------------

def gt_discover_tokens(chain: str = 'base', limit: int = 20, min_volume_24h: float = 50_000,
                       min_liquidity: float = 100_000) -> list[dict]:
    """
    Discover active tokens on Base from trending + high-volume pools.
    Returns list of token dicts ready for pipeline processing.

    Each token dict includes:
      address, symbol, name, volume_24h, market_cap_usd,
      pool_address, liquidity_usd, price_usd, source
    """
    seen: dict = {}

    # Fetch 2 pages of trending pools (20 tokens per page = 40 candidates)
    for page in range(1, 3):
        try:
            data = _gt_get(
                f'https://api.geckoterminal.com/api/v2/networks/{chain}/trending_pools',
                params={'page': page, 'include': 'base_token'}
            )
        except Exception as e:
            print(f'[GT] trending_pools page {page} failed: {e}')
            break

        pools = data.get('data', [])
        # Build address→token map from included objects
        included_tokens = {}
        for inc in data.get('included', []):
            if inc.get('type') == 'token':
                inc_addr = inc.get('attributes', {}).get('address', '').lower()
                if inc_addr:
                    included_tokens[inc_addr] = inc.get('attributes', {})

        for pool in pools:
            attrs = pool.get('attributes', {})
            rels = pool.get('relationships', {})

            # Get base token address from relationship
            base_token_rel = rels.get('base_token', {}).get('data', {})
            # token id format: "base_0xADDRESS"
            token_id = base_token_rel.get('id', '')
            token_addr = token_id.split('_', 1)[-1].lower() if '_' in token_id else ''
            if not token_addr:
                continue

            symbol = included_tokens.get(token_addr, {}).get('symbol', attrs.get('name', '').split('/')[0].strip())
            if not symbol or symbol.upper() in _SKIP_SYMBOLS:
                continue

            vol_24h = float(attrs.get('volume_usd', {}).get('h24', 0) or 0)
            liquidity = float(attrs.get('reserve_in_usd', 0) or 0)
            price = float(attrs.get('base_token_price_usd', 0) or 0)
            mc = float(attrs.get('market_cap_usd', 0) or 0)
            pool_address = attrs.get('address', '').lower()

            if vol_24h < min_volume_24h or liquidity < min_liquidity:
                continue

            if token_addr in seen:
                # Keep entry with highest liquidity
                if liquidity > seen[token_addr].get('liquidity_usd', 0):
                    seen[token_addr].update({'pool_address': pool_address, 'liquidity_usd': liquidity})
                continue

            seen[token_addr] = {
                'address': token_addr,
                'symbol': symbol,
                'name': symbol,
                'volume_24h': vol_24h,
                'market_cap_usd': mc,
                'pool_address': pool_address,
                'liquidity_usd': liquidity,
                'price_usd': price,
                'holders_count': 0,
                'source': 'gt_trending',
            }

        if len(seen) >= limit:
            break

    results = list(seen.values())[:limit]
    print(f'[GT] Discovered {len(results)} tokens from trending pools')
    return results


# ---------------------------------------------------------------------------
# OHLCV (disk-cached daily)
# ---------------------------------------------------------------------------

def _ohlcv_cache_entry_is_fresh(entry: dict) -> bool:
    """Return True if OHLCV cache entry was fetched today (UTC)."""
    ts_str = entry.get('cached_date', '')
    if not ts_str:
        return False
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return ts_str == today


def _load_ohlcv_cache() -> dict:
    try:
        return json.loads(_GT_OHLCV_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_ohlcv_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _GT_OHLCV_CACHE_FILE.write_text(json.dumps(cache))


def gt_get_ohlcv(pool_address: str, token_address: str = '', chain: str = 'base',
                 days: int = 30) -> list[dict]:
    """
    Fetch daily OHLCV candles for a pool from GeckoTerminal.
    Disk-cached with daily TTL (free API, refreshed once per UTC day).

    Returns candles in same format as Nansen get_price_ohlcv():
      [{'timestamp': int, 'open': float, 'high': float, 'low': float,
        'close': float, 'volume': float}, ...]
    """
    if not pool_address:
        return []

    cache_key = pool_address.lower()
    cache = _load_ohlcv_cache()
    entry = cache.get(cache_key)
    if entry and _ohlcv_cache_entry_is_fresh(entry):
        return entry.get('candles', [])

    # Cache miss — fetch from API
    try:
        data = _gt_get(
            f'https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pool_address}/ohlcv/day',
            params={'limit': days, 'currency': 'usd'}
        )
    except Exception as e:
        print(f'[GT] OHLCV fetch failed for {pool_address}: {e}')
        return []

    raw = data.get('data', {}).get('attributes', {}).get('ohlcv_list', [])
    candles = []
    for item in raw:
        # item = [timestamp_ms, open, high, low, close, volume]
        if len(item) < 6:
            continue
        ts, o, h, lo, c, vol = item
        # GT returns timestamp in seconds already (not ms)
        candles.append({
            'timestamp': int(ts),
            'open':   float(o  or 0),
            'high':   float(h  or 0),
            'low':    float(lo or 0),
            'close':  float(c  or 0),
            'volume': float(vol or 0),
        })

    if candles:
        cache[cache_key] = {
            'candles': candles,
            'cached_date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        }
        _save_ohlcv_cache(cache)
        print(f'[GT] OHLCV cached: {len(candles)} candles for {pool_address[:10]}...')

    return candles


# ---------------------------------------------------------------------------
# Token info (price + liquidity lookup for tokens not from discovery)
# ---------------------------------------------------------------------------

def _load_price_cache() -> dict:
    try:
        return json.load(open(_PRICE_CACHE_FILE))
    except Exception:
        return {}


def _save_price_cache(cache: dict) -> None:
    try:
        _PRICE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        json.dump(cache, open(_PRICE_CACHE_FILE, 'w'))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fallback price source: pool_cache (permanent DexScreener data)
# ---------------------------------------------------------------------------

def _pool_cache_price_fallback(token_address: str) -> dict:
    """
    Fetch token price and liquidity from pool_cache.json (permanent DexScreener cache).
    Returns partial dict compatible with gt_get_token_info output.
    Returns empty dict if token not in cache.
    """
    try:
        from pool_resolver import get_pool_info
        info = get_pool_info(token_address)
        if not info or not info.get('price_usd'):
            return {}
        return {
            'price_usd': info.get('price_usd', 0),
            'volume_24h': 0.0,  # pool_cache doesn't store volume
            'liquidity_usd': info.get('liquidity_usd', 0),
            'market_cap_usd': 0.0,
            'top_pool_address': info.get('pool_address', ''),
            '_source': 'pool_cache',
        }
    except Exception:
        return {}


def gt_get_token_info(token_address: str, chain: str = 'base') -> dict:
    """
    Fetch token price, volume_24h, liquidity, market_cap from GeckoTerminal.
    Also returns top_pool_address for subsequent OHLCV calls.

    Returns: {price_usd, volume_24h, liquidity_usd, market_cap_usd, top_pool_address}
    Returns empty dict on error.

    Results are cached to disk for _PRICE_CACHE_TTL seconds so multiple
    processes (verifier, pipeline, daily_summary) don't hammer GT with
    duplicate requests and trigger 429s.
    """
    cache_key = f'{chain}:{token_address.lower()}'
    cache = _load_price_cache()
    now_ts = time.time()
    if cache_key in cache:
        entry = cache[cache_key]
        if now_ts - entry.get('cached_at', 0) < _PRICE_CACHE_TTL:
            return entry.get('data', {})

    # Primary: GeckoTerminal (rate-limited but has OHLCV metadata)
    try:
        data = _gt_get(
            f'https://api.geckoterminal.com/api/v2/networks/{chain}/tokens/{token_address}',
            params={'include': 'top_pools'}
        )
    except Exception as e:
        print(f'[GT] token_info failed for {token_address}: {e}')
        # Fallback: pool_cache (permanent DexScreener data via pool_resolver)
        info = _pool_cache_price_fallback(token_address)
        if info:
            cache[cache_key] = {'cached_at': now_ts, 'data': info}
            _save_price_cache(cache)
            return info
        # Fallback 2: stale disk cache
        if cache_key in cache:
            age_s = time.time() - cache[cache_key].get('cached_at', 0)
            print(f'[GT] stale cache fallback for {token_address} (age {age_s/60:.0f}m)')
            return cache[cache_key].get('data', {})
        return {}

    attrs = data.get('data', {}).get('attributes', {})
    price   = float(attrs.get('price_usd', 0) or 0)
    vol     = float(attrs.get('volume_usd', {}).get('h24', 0) or 0)
    mc      = float(attrs.get('market_cap_usd', 0) or 0)
    liq     = float(attrs.get('total_reserve_in_usd', 0) or 0)

    # Get top pool address from included
    top_pool = ''
    included = data.get('included', [])
    if included:
        top_pool = included[0].get('attributes', {}).get('address', '').lower()

    result = {
        'price_usd': price,
        'volume_24h': vol,
        'liquidity_usd': liq,
        'market_cap_usd': mc,
        'top_pool_address': top_pool,
    }

    # Write to shared price cache
    cache[cache_key] = {'cached_at': now_ts, 'data': result}
    _save_price_cache(cache)

    return result
