"""
scanner.py - Nansen API wrapper with disk-based caching

Provides functions to fetch token data from Nansen API:
- get_price_ohlcv: OHLCV price data (disk cache, refreshed daily)
- get_sm_netflow: Smart money netflows (from screener cache -- free)
- get_top_holders: Top holder concentration (disk cache, refreshed every 4h)
- get_pool_liquidity: Pool liquidity via web3.py (free, on-chain)
- discover_sm_tokens: Dynamic SM token discovery (from screener cache -- free)

CREDIT BUDGET DESIGN:
  Every 30-min run: 1 screener call = 2 credits (disk-cached, refreshed when stale)
  Top holders: disk-cached 4h -- ~10 credits per token per 4h window
  OHLCV: disk-cached daily -- ~2 credits per token per day
  Dead endpoints removed: netflow bulk, holdings bulk, dex-trades bulk

Mock mode is active when NANSEN_MOCK=1 OR NANSEN_API_KEY is not set.

AUTH RULES (critical - wrong header = silent 404s):
  header name: 'apikey'  (lowercase - NOT 'apiKey')
  base url:    https://api.nansen.ai/api/v1/
  method:      POST with JSON body (all endpoints)
"""

import os
import json
import random
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional


# Symbols to skip (stablecoins, wrapped assets)
_SKIP_SYMBOLS = {"USDC", "USDT", "DAI", "USDS", "WETH", "WBTC", "cbBTC", "cbETH"}

DATA_DIR = Path(__file__).parent.parent / 'data'

# Shared directory for multi-chain cache
SHARED_DIR = Path(__file__).parent.parent.parent / 'shared'

# Disk cache paths
_SCREENER_CACHE_FILE = SHARED_DIR / 'nansen_cache.json'
_SCREENER_CACHE_TMP = SHARED_DIR / 'nansen_cache.json.tmp'
_HOLDERS_CACHE_FILE = DATA_DIR / 'holders_cache.json'
_HOLDERS_CACHE_TMP = DATA_DIR / 'holders_cache.json.tmp'
_FLOW_INTELLIGENCE_CACHE_FILE = DATA_DIR / 'flow_intelligence_cache.json'
_FLOW_INTELLIGENCE_CACHE_TMP = DATA_DIR / 'flow_intelligence_cache.json.tmp'
_SM_DISCOVERY_CACHE_FILE = DATA_DIR / 'base_sm_discovery_cache.json'

# Cache TTLs
_SCREENER_TTL_SECONDS = 14400  # 4 hours -- SM netflow is a 24h measurement, doesn't need 30-min refresh
_HOLDERS_TTL_SECONDS = 14400   # 4 hours -- holder concentration is slow-moving
_FLOW_INTELLIGENCE_TTL_SECONDS = 14400  # 4 hours -- flow intelligence is slow-moving
_SM_DISCOVERY_TTL_SECONDS = 3600  # 1 hour -- SM discovery refresh rate


def _is_mock_mode() -> bool:
    """Check if mock mode is active."""
    return os.environ.get('NANSEN_MOCK') == '1' or not os.environ.get('NANSEN_API_KEY')


def _headers() -> dict:
    """Return correct Nansen auth headers. apikey MUST be lowercase."""
    return {
        'apikey': os.environ['NANSEN_API_KEY'],
        'Content-Type': 'application/json',
    }


def log_api_error(source: str, error: str, tokens_loaded: int) -> None:
    """Append a structured error log to data/api_error_log.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / 'api_error_log.jsonl'
    entry = {
        'ts': datetime.utcnow().isoformat(),
        'source': source,
        'error': error,
        'tokens_loaded': tokens_loaded,
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + '\n')


# ---------------------------------------------------------------------------
# Screener cache (disk-persisted, 30-min TTL)
# One bulk call covers: discovery, netflow, price, volume for all Base tokens.
# ---------------------------------------------------------------------------

def _screener_cache_is_fresh() -> bool:
    """Return True if disk screener cache exists and is younger than TTL."""
    if not _SCREENER_CACHE_FILE.exists():
        return False
    age = time.time() - _SCREENER_CACHE_FILE.stat().st_mtime
    return age < _SCREENER_TTL_SECONDS


def _load_screener_from_disk() -> dict:
    """Load screener cache from disk. Returns {} on any error."""
    try:
        return json.loads(_SCREENER_CACHE_FILE.read_text())
    except Exception:
        return {}


def _refresh_screener_cache() -> dict:
    """Fetch fresh screener data for base+solana, write atomically to shared cache."""
    import requests
    cache = {}
    credits_used = 0
    try:
        page = 1
        max_pages = 10
        while page <= max_pages:
            r = requests.post(
                'https://api.nansen.ai/api/v1/token-screener',
                headers=_headers(),
                json={'chains': ['base', 'solana'], 'timeframe': '24h',
                      'pagination': {'limit': 10, 'page': page}},
                timeout=30,
            )
            r.raise_for_status()
            resp = r.json()
            items = resp.get('data', [])
            is_last = resp.get('is_last_page', False)
            credits_used += 2
            for item in items:
                raw_addr = item.get('token_address', '')
                if not raw_addr:
                    continue
                chain = item.get('chain', '')
                # Solana addresses are case-sensitive base58 - preserve original case
                # EVM addresses are case-insensitive - lowercase for consistent lookup
                if chain == 'solana':
                    addr = raw_addr
                else:
                    addr = raw_addr.lower()
                cache[addr] = item
            if is_last or not items:
                break
            page += 1
        # Atomic write: write to .tmp then os.replace()
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        _SCREENER_CACHE_TMP.write_text(json.dumps(cache))
        import os as _os
        _os.replace(str(_SCREENER_CACHE_TMP), str(_SCREENER_CACHE_FILE))
        print(f'[SCREENER] Refreshed shared cache: {len(cache)} tokens ({credits_used} credits, {page} pages)')
    except Exception as e:
        print(f'[SCREENER] Refresh failed: {e}')
    return cache


def _get_screener_cache(chain: str = 'base') -> dict:
    """Return screener cache dict filtered to the requested chain."""
    if _screener_cache_is_fresh():
        data = _load_screener_from_disk()
        if data:
            print(f'[SCREENER] Read cache: {len(data)} tokens')
            return {k: v for k, v in data.items() if v.get('chain') == chain}
    full = _refresh_screener_cache()
    return {k: v for k, v in full.items() if v.get('chain') == chain}


def get_screener_data(token_address: str, chain: str = 'base') -> dict:
    """
    Return screener row for a token from the 24h screener cache.
    Fields: netflow, liquidity, buy_volume, sell_volume, volume, market_cap_usd, price_usd

    Returns empty dict if token not in screener.
    """
    if _is_mock_mode():
        return {}
    cache = _get_screener_cache(chain)
    if not cache:
        log_api_error('screener', 'empty response - 0 tokens loaded', 0)
    return cache.get(token_address.lower(), {})


# ---------------------------------------------------------------------------
# SM Netflow -- free, derived from screener cache
# ---------------------------------------------------------------------------

def get_sm_netflow(token_address: str, chain: str = 'base', window_hours: int = 24, enrich: bool = False,
                   trader_count: int = None, netflow_24h_usd: float = None) -> dict:
    """
    Return SM netflow for a token.

    Data sources (in priority order):
    1. Explicit params (trader_count, netflow_24h_usd) — passed from SM discovery, most reliable
    2. Screener cache (netflow field) — shared cache, may not have all tokens

    If enrich=True, calls get_flow_intelligence() for multi-timeframe data.

    Returns: {
        'net_flow_1h_usd': float,
        'net_flow_24h_usd': float,
        'net_flow_7d_usd': float,
        'net_flow_30d_usd': float,
        'trader_count': int,
        'is_positive': bool,
        'netflow_usd': float,       # backward compat (24h)
        'window_hours': int,
    }
    """
    if _is_mock_mode():
        print(f'[MOCK] get_sm_netflow called for {token_address}')
        return {
            'net_flow_1h_usd': 50000.0,
            'net_flow_24h_usd': 125000.0,
            'net_flow_7d_usd': 300000.0,
            'net_flow_30d_usd': 800000.0,
            'trader_count': 12,
            'is_positive': True,
            'netflow_usd': 125000.0,
            'window_hours': window_hours,
        }

    # Priority 1: explicit params from SM discovery call (most reliable)
    if netflow_24h_usd is not None and trader_count is not None:
        net_flow_24h = float(netflow_24h_usd)
        return {
            'net_flow_1h_usd': 0.0,
            'net_flow_24h_usd': net_flow_24h,
            'net_flow_7d_usd': 0.0,
            'net_flow_30d_usd': 0.0,
            'trader_count': int(trader_count),
            'is_positive': net_flow_24h > 0,
            'netflow_usd': net_flow_24h,
            'window_hours': window_hours,
        }

    # Priority 2: screener cache fallback
    screener = get_screener_data(token_address, chain)
    net_flow_24h_usd_fallback = float(screener.get('netflow', 0) or 0)
    is_positive = net_flow_24h_usd_fallback > 0
    trader_count_fallback = 0  # screener doesn't have trader_count

    result = {
        'net_flow_1h_usd': 0.0,
        'net_flow_24h_usd': net_flow_24h_usd_fallback,
        'net_flow_7d_usd': 0.0,
        'net_flow_30d_usd': 0.0,
        'trader_count': trader_count_fallback,
        'is_positive': is_positive,
        'netflow_usd': net_flow_24h_usd_fallback,
        'window_hours': window_hours,
    }

    # If enrich=True, call get_flow_intelligence() for multi-timeframe data
    if enrich:
        flow_data = get_flow_intelligence(token_address, chain)
        if flow_data and flow_data.get('net_flow_7d_usd', 0) != 0:
            # Use flow intelligence data if available
            result['net_flow_1h_usd'] = flow_data.get('net_flow_1h_usd', 0.0)
            result['net_flow_7d_usd'] = flow_data.get('net_flow_7d_usd', 0.0)
            result['net_flow_30d_usd'] = flow_data.get('net_flow_30d_usd', 0.0)

    return result


# ---------------------------------------------------------------------------
# Flow Intelligence (disk cache, 4-hour TTL)
# ---------------------------------------------------------------------------

def _flow_intelligence_cache_entry_is_fresh(entry: dict) -> bool:
    """Return True if a flow intelligence cache entry is younger than TTL."""
    ts_str = entry.get('cached_at', '')
    if not ts_str:
        return False
    try:
        cached_at = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        return age < _FLOW_INTELLIGENCE_TTL_SECONDS
    except Exception:
        return False


def _load_flow_intelligence_cache() -> dict:
    """Load flow intelligence disk cache. Returns {} on any error."""
    try:
        return json.loads(_FLOW_INTELLIGENCE_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_flow_intelligence_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import os as _os
    _FLOW_INTELLIGENCE_CACHE_TMP.write_text(json.dumps(cache))
    _os.replace(str(_FLOW_INTELLIGENCE_CACHE_TMP), str(_FLOW_INTELLIGENCE_CACHE_FILE))


def get_flow_intelligence(token_address: str, chain: str = 'base') -> dict:
    """
    Fetch flow intelligence data for a token (multi-timeframe buy/sell pressure).
    Disk-cached with 4-hour TTL (10 credits per call -- only paid on cache miss).

    Endpoint: POST /api/v1/flow-intelligence
    Returns: {
        'net_flow_1h_usd': float,
        'net_flow_24h_usd': float,
        'net_flow_7d_usd': float,
        'net_flow_30d_usd': float,
        'buy_pressure_pct': float,
        'sell_pressure_pct': float,
    }
    """
    if _is_mock_mode():
        print(f'[MOCK] get_flow_intelligence called for {token_address}')
        return {
            'net_flow_1h_usd': 50000.0,
            'net_flow_24h_usd': 125000.0,
            'net_flow_7d_usd': 300000.0,
            'net_flow_30d_usd': 800000.0,
            'buy_pressure_pct': 65.0,
            'sell_pressure_pct': 35.0,
        }

    addr_key = token_address.lower()

    # Check disk cache
    cache = _load_flow_intelligence_cache()
    entry = cache.get(addr_key)
    if entry and _flow_intelligence_cache_entry_is_fresh(entry):
        return {
            'net_flow_1h_usd': entry['net_flow_1h_usd'],
            'net_flow_24h_usd': entry['net_flow_24h_usd'],
            'net_flow_7d_usd': entry['net_flow_7d_usd'],
            'net_flow_30d_usd': entry['net_flow_30d_usd'],
            'buy_pressure_pct': entry['buy_pressure_pct'],
            'sell_pressure_pct': entry['sell_pressure_pct'],
        }

    # Cache miss -- fetch from API (10 credits)
    import requests
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/flow-intelligence',
            headers=_headers(),
            json={'token_address': token_address, 'chain': chain},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get('data', {})

        # Parse response to extract multi-timeframe net flow data
        result = {
            'net_flow_1h_usd': float(data.get('net_flow_1h_usd', 0) or 0),
            'net_flow_24h_usd': float(data.get('net_flow_24h_usd', 0) or 0),
            'net_flow_7d_usd': float(data.get('net_flow_7d_usd', 0) or 0),
            'net_flow_30d_usd': float(data.get('net_flow_30d_usd', 0) or 0),
            'buy_pressure_pct': float(data.get('buy_pressure_pct', 0) or 0),
            'sell_pressure_pct': float(data.get('sell_pressure_pct', 0) or 0),
        }

        # Write to disk cache
        cache[addr_key] = {
            **result,
            'cached_at': datetime.now(timezone.utc).isoformat(),
        }
        _save_flow_intelligence_cache(cache)
        print(f'[FLOW INTELLIGENCE] Fetched {token_address[:10]}... (10 credits)')
        return result
    except Exception as e:
        log_api_error('flow_intelligence', str(e), 0)
        return {
            'net_flow_1h_usd': 0.0,
            'net_flow_24h_usd': 0.0,
            'net_flow_7d_usd': 0.0,
            'net_flow_30d_usd': 0.0,
            'buy_pressure_pct': 0.0,
            'sell_pressure_pct': 0.0,
        }


# ---------------------------------------------------------------------------
# OHLCV (disk cache via get_price_ohlcv -- already cached daily by pipeline)
# ---------------------------------------------------------------------------

def get_price_ohlcv(token_address: str, chain: str = 'base', interval: str = '1d', limit: int = 14) -> list[dict]:
    """
    Fetch OHLCV price data for a token.

    Endpoint: POST /api/v1/tgm/token-ohlcv
    Body: {"token_address": str, "chain": str, "timeframe": str}
    Note: API uses 'timeframe' (not 'interval'); date defaults to last 30 days.

    Returns: list of OHLCV dicts - same format as support_resistance.calculate_sr() expects:
        [{'timestamp': int, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}, ...]
    """
    if _is_mock_mode():
        print(f'[MOCK] get_price_ohlcv called for {token_address}')
        base_price = 1.50
        candles = []
        current_price = base_price
        now = int(time.time())
        for i in range(30):
            change_pct = random.uniform(-0.08, 0.08)
            open_price = current_price
            close_price = current_price * (1 + change_pct)
            high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.05))
            low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.05))
            volume = random.uniform(50000, 200000)
            candles.append({
                'timestamp': now - (30 - i) * 86400,
                'open':   round(open_price, 6),
                'high':   round(high_price, 6),
                'low':    round(low_price, 6),
                'close':  round(close_price, 6),
                'volume': round(volume, 2)
            })
            current_price = close_price
        return candles

    import requests
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/tgm/token-ohlcv',
            headers=_headers(),
            json={'token_address': token_address, 'chain': chain, 'timeframe': interval},
            timeout=30,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        if e.response.status_code in (422, 404):
            print(f'[SKIP OHLCV] No data for {token_address}')
            return []
        raise
    candles = []
    for item in r.json().get('data', []):
        ts_raw = item.get('interval_start', item.get('timestamp', ''))
        if isinstance(ts_raw, str):
            from datetime import datetime as _dt
            try:
                ts = int(_dt.fromisoformat(ts_raw).replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                ts = 0
        else:
            ts = int(ts_raw or 0)
        o  = float(item.get('open')  or 0)
        h  = float(item.get('high')  or 0)
        lo = float(item.get('low')   or 0)
        c  = float(item.get('close') or 0)
        if o == 0 and h == 0 and lo == 0 and c == 0:
            continue
        candles.append({
            'timestamp': ts,
            'open':   o,
            'high':   h,
            'low':    lo,
            'close':  c,
            'volume': float(item.get('volume_usd') or item.get('volume') or 0),
        })
    return candles


# ---------------------------------------------------------------------------
# Top Holders (disk cache, 4-hour TTL)
# ---------------------------------------------------------------------------

def _holders_cache_entry_is_fresh(entry: dict) -> bool:
    """Return True if a holders cache entry is younger than TTL."""
    ts_str = entry.get('cached_at', '')
    if not ts_str:
        return False
    try:
        cached_at = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        return age < _HOLDERS_TTL_SECONDS
    except Exception:
        return False


def _load_holders_cache() -> dict:
    """Load holders disk cache. Returns {} on any error."""
    try:
        return json.loads(_HOLDERS_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_holders_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import os as _os
    _HOLDERS_CACHE_TMP.write_text(json.dumps(cache))
    _os.replace(str(_HOLDERS_CACHE_TMP), str(_HOLDERS_CACHE_FILE))


def get_top_holders(token_address: str, chain: str = 'base', top_n: int = 5) -> dict:
    """
    Fetch top holder concentration data for a token.
    Disk-cached with 4-hour TTL (10 credits per call -- only paid on cache miss).

    Endpoint: POST /api/v1/tgm/holders
    Returns: {'top5_pct': float, 'concentration_risk': bool}
    concentration_risk = True if top5_pct > 50
    """
    if _is_mock_mode():
        print(f'[MOCK] get_top_holders called for {token_address}')
        return {'top5_pct': 38.2, 'concentration_risk': False}

    addr_key = token_address.lower()

    # Check disk cache
    cache = _load_holders_cache()
    entry = cache.get(addr_key)
    if entry and _holders_cache_entry_is_fresh(entry):
        return {'top5_pct': entry['top5_pct'], 'concentration_risk': entry['concentration_risk']}

    # Cache miss -- fetch from API (10 credits)
    import requests
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/tgm/holders',
            headers=_headers(),
            json={'token_address': token_address, 'chain': chain},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get('data', [])
        if isinstance(data, list):
            top5_pct = sum(float(h.get('percentage', h.get('pct', 0))) for h in data[:top_n])
        else:
            top5_pct = float(data.get('top5_pct', data.get('top_holders_pct', 0)))
        result = {
            'top5_pct': top5_pct,
            'concentration_risk': top5_pct > 50,
        }

        # Write to disk cache
        cache[addr_key] = {
            **result,
            'cached_at': datetime.now(timezone.utc).isoformat(),
        }
        _save_holders_cache(cache)
        return result
    except Exception as e:
        log_api_error('holders', str(e), 0)
        return {'top5_pct': 0.0, 'concentration_risk': False}


# ---------------------------------------------------------------------------
# SM Discovery for Base chain — smart-money/netflow with 1h disk cache
# ---------------------------------------------------------------------------

def _sm_discovery_cache_is_fresh() -> bool:
    """Return True if SM discovery cache exists and is younger than TTL."""
    if not _SM_DISCOVERY_CACHE_FILE.exists():
        return False
    age = time.time() - _SM_DISCOVERY_CACHE_FILE.stat().st_mtime
    return age < _SM_DISCOVERY_TTL_SECONDS


def _load_sm_discovery_from_disk() -> list:
    """Load SM discovery candidates from disk cache. Returns [] on any error."""
    try:
        return json.loads(_SM_DISCOVERY_CACHE_FILE.read_text())
    except Exception:
        return []


def _save_sm_discovery_to_disk(candidates: list) -> None:
    """Write candidates to disk cache atomically."""
    tmp = _SM_DISCOVERY_CACHE_FILE.with_suffix('.tmp')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(candidates))
    tmp.replace(str(_SM_DISCOVERY_CACHE_FILE))


def _enrich_from_screener_cache(candidates: list) -> None:
    """
    Enrich each candidate with liquidity, volume, market_cap, price from
    the shared screener cache (free, no extra API call).
    Tokens still at $0 liquidity are flagged for DexScreener enrichment.
    Mutates candidates in place.
    """
    if _is_mock_mode():
        return
    if _screener_cache_is_fresh():
        screener = _load_screener_from_disk()
    else:
        screener = _refresh_screener_cache()

    screener_base = {k: v for k, v in screener.items() if v.get('chain') == 'base'}
    needs_dexscreener = []
    for c in candidates:
        addr = c['address'].lower()
        row = screener_base.get(addr, {})
        c['liquidity_usd'] = float(row.get('liquidity', 0) or 0)
        c['volume_24h'] = float(row.get('volume', 0) or 0)
        c['market_cap_usd'] = float(row.get('market_cap_usd', 0) or 0)
        c['price_usd'] = float(row.get('price_usd', 0) or 0)
        if c['liquidity_usd'] == 0:
            needs_dexscreener.append(c)

    # DexScreener batch enrichment for tokens missing from screener cache (free)
    if needs_dexscreener:
        _enrich_liquidity_dexscreener(needs_dexscreener)


def _enrich_liquidity_dexscreener(candidates: list) -> None:
    """
    Fetch liquidity from DexScreener for candidates with $0 liquidity.
    Batch request — single API call for all candidates. Free.
    Mutates candidates in place.
    """
    if not candidates:
        return
    try:
        import requests
        addrs = [c['address'] for c in candidates]
        addr_str = ','.join(addrs)
        r = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{addr_str}',
            headers={'Accept': 'application/json'},
            timeout=15
        )
        if r.status_code != 200:
            print(f'[SM DISCOVERY] DexScreener enrichment failed: {r.status_code}')
            return
        pairs_data = r.json().get('pairs', [])
        if isinstance(pairs_data, dict):
            pairs_data = pairs_data.get('pairs', [])
        liq_map = {}
        price_map = {}
        for pair in pairs_data:
            if not isinstance(pair, dict):
                continue
            base = pair.get('baseToken', {})
            quote = pair.get('quoteToken', {})
            if not base or not quote:
                continue
            pair_addr = (base.get('address') or '').lower()
            liq = float(pair.get('liquidity', {}).get('usd', 0) or 0)
            price = float(pair.get('priceUsd', 0) or 0)
            if liq > liq_map.get(pair_addr, 0):
                liq_map[pair_addr] = liq
            if price > 0 and price_map.get(pair_addr, 0) == 0:
                price_map[pair_addr] = price
        for c in candidates:
            addr = c['address'].lower()
            if c['liquidity_usd'] == 0:
                c['liquidity_usd'] = liq_map.get(addr, 0)
            if c['price_usd'] == 0:
                c['price_usd'] = price_map.get(addr, 0)
    except Exception as e:
        print(f'[SM DISCOVERY] DexScreener enrichment error: {e}')


def load_candidate_tokens_base(config: dict = None) -> list:
    """
    Load Base tokens from Nansen smart-money/netflow API.
    Discovery: 1 page (50 tokens) via smart-money/netflow, cached 1 hour.
    Liquidity/enrichment: from shared screener cache (free, no extra API call).

    Returns token dicts in the shape run_pipeline.py expects:
      {address, symbol, name, volume_24h, market_cap_usd, pool_address,
       liquidity_usd, price_usd, holders_count, source, trader_count,
       netflow_24h_usd, chain}

    Falls back to [] if Nansen call fails (pipeline handles fallback to GeckoTerminal).
    """
    if _is_mock_mode():
        return []

    # Check disk cache first
    if _sm_discovery_cache_is_fresh():
        cached = _load_sm_discovery_from_disk()
        if cached:
            print(f'[SM DISCOVERY] Cache hit: {len(cached)} Base candidates (fresh)')
            return cached

    # Cache miss — fetch 1 page from Nansen
    import requests
    candidates = []
    seen = set()
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/smart-money/netflow',
            headers=_headers(),
            json={
                'chains': ['base'],
                'filters': {
                    'include_stablecoins': False,
                    'include_native_tokens': False,
                },
                'pagination': {'page': 1, 'per_page': 50},
                'order_by': [{'field': 'trader_count', 'direction': 'DESC'}]
            },
            timeout=30
        )
        if r.status_code != 200:
            print(f'[SM DISCOVERY] Nansen error: {r.status_code} - {r.text[:100]}')
        else:
            items = r.json().get('data', [])
            for item in items:
                addr = (item.get('token_address') or '').lower()
                sym = (item.get('token_symbol') or '').upper()
                if not addr or not sym or sym in seen:
                    continue
                seen.add(sym)
                if sym in _SKIP_SYMBOLS:
                    continue
                mc = float(item.get('market_cap_usd', 0) or 0)
                if mc < 1_000_000:  # Skip micro-caps
                    continue
                candidates.append({
                    'address': addr,
                    'symbol': sym,
                    'name': sym,
                    'volume_24h': 0.0,  # enriched from screener cache below
                    'market_cap_usd': mc,
                    'pool_address': '',   # pipeline will look up if needed
                    'liquidity_usd': 0.0,
                    'price_usd': float(item.get('price_usd', 0) or 0),
                    'holders_count': int(item.get('trader_count', 0) or 0),
                    'source': 'nansen_sm_netflow',
                    'trader_count': int(item.get('trader_count', 0) or 0),
                    'netflow_24h_usd': float(item.get('net_flow_24h_usd', 0) or 0),
                    'chain': 'base',
                    'token_age_days': int(item.get('token_age_days', 0) or 0),
                })
    except Exception as e:
        print(f'[SM DISCOVERY] Request error: {e}')

    # Enrich liquidity/volume/price from screener cache (free)
    _enrich_from_screener_cache(candidates)

    # Sort by trader_count descending
    candidates.sort(key=lambda x: x['trader_count'], reverse=True)

    # Save to disk cache
    _save_sm_discovery_to_disk(candidates)
    print(f'[SM DISCOVERY] Cache miss: fetched {len(candidates)} candidates, cached 1h')

    return candidates


# ---------------------------------------------------------------------------
