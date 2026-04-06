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

# Unified logging — emit("discovery", data) summaries to base_trader_log.jsonl
sys_path_added = False
try:
    from _ulogging import emit
except ImportError:
    emit = None  # graceful fallback if not yet deployed


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
_SM_HOLDINGS_CACHE_FILE = DATA_DIR / 'sm_holdings_cache.json'
_SM_HOLDINGS_CACHE_TMP = DATA_DIR / 'sm_holdings_cache.json.tmp'

# Cache TTLs
_SCREENER_TTL_SECONDS = 14400   # 4 hours -- SM netflow is a 24h measurement, doesn't need 30-min refresh
_HOLDERS_TTL_SECONDS = 43200    # 12 hours -- prevents overnight cache starvation; 22 tokens × 10 credits = 220 per cold run, too expensive to hit daily
_FLOW_INTELLIGENCE_TTL_SECONDS = 14400  # 4 hours -- flow intelligence is slow-moving
_SM_DISCOVERY_TTL_SECONDS = 14400  # 4 hours -- extended from 1h to reduce discovery API calls; SM holdings don't change minute-to-minute
_SM_HOLDINGS_TTL_SECONDS = 14400     # 4 hours -- bulk SM wallet holdings per token; doesn't change minute-to-minute


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
        'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
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
    # and get_sm_holdings() for real trader_count (both disk-cached after pre-warm)
    if enrich:
        # Populate real trader_count from SM holdings cache (pre-warmed at startup,
        # or fetched here on first call — one API call, shared by all subsequent calls)
        holdings_data = get_sm_holdings(token_address, chain)
        if holdings_data:
            result['trader_count'] = int(holdings_data.get('holders_count', 0))

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

    Endpoint: POST /api/v1/tgm/flow-intelligence
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
            'https://api.nansen.ai/api/v1/tgm/flow-intelligence',
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
# SM Holdings -- bulk SM wallet counts per token (disk cache, 4-hour TTL)
# ---------------------------------------------------------------------------

def _sm_holdings_cache_is_fresh() -> bool:
    """Return True if SM holdings cache exists and is younger than TTL."""
    if not _SM_HOLDINGS_CACHE_FILE.exists():
        return False
    age = time.time() - _SM_HOLDINGS_CACHE_FILE.stat().st_mtime
    return age < _SM_HOLDINGS_TTL_SECONDS


def _load_sm_holdings_cache() -> dict:
    """Load SM holdings disk cache. Returns {} on any error."""
    try:
        raw = json.loads(_SM_HOLDINGS_CACHE_FILE.read_text())
        if isinstance(raw, list):
            # v1 bare dict — treat as stale
            return {}
        return raw.get('holdings', {})
    except Exception:
        return {}


def _save_sm_holdings_cache(holdings: dict) -> None:
    """Write SM holdings to disk cache atomically. Wraps bare dict in v2 envelope."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {'updated_at': now, 'holdings': holdings}
    tmp = _SM_HOLDINGS_CACHE_TMP
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload))
    tmp.replace(str(_SM_HOLDINGS_CACHE_FILE))


def get_sm_holdings(token_address: str = None, chain: str = 'base') -> dict:
    """
    Fetch SM wallet holdings for a token (or all tokens on chain).

    Disk-cached with 4-hour TTL. One API call returns all tokens on the chain,
    so we cache the full dict and return per-token data from it.

    Endpoint: POST /api/v1/smart-money/holdings
    Body: {"chains": ["base"]}  (chains takes a list, not a string)
    Response fields: chain, token_address, token_symbol, value_usd, holders_count,
                    balance_24h_percent_change, token_age_days
    Credits: 5 per call (from Nansen skill)

    If token_address is provided, returns just that token's data (or {} if not found).
    """
    if _is_mock_mode():
        print(f'[MOCK] get_sm_holdings called for {token_address}')
        return {
            token_address: {'holders_count': 12, 'value_usd': 125000.0, 'token_symbol': 'MOCK'}
        }

    # Check disk cache
    if _sm_holdings_cache_is_fresh():
        holdings = _load_sm_holdings_cache()
        if holdings:
            if token_address:
                return holdings.get(token_address.lower(), {})
            return holdings

    # Cache miss -- fetch bulk SM holdings from Nansen (5 credits)
    import requests
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/smart-money/holdings',
            headers=_headers(),
            json={'chains': [chain]},   # chains takes a list: ["base"], not {"chain": "base"}
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get('data', [])

        holdings = {}
        if isinstance(data, list):
            for item in data:
                addr = (item.get('token_address') or '').lower()
                if not addr:
                    continue
                holdings[addr] = {
                    'holders_count': int(item.get('holders_count', 0) or 0),
                    'value_usd': float(item.get('value_usd', 0) or 0),
                    'token_symbol': item.get('token_symbol', ''),
                    'balance_24h_percent_change': float(item.get('balance_24h_percent_change', 0) or 0),
                    'token_age_days': float(item.get('token_age_days', 0) or 0),
                }

        _save_sm_holdings_cache(holdings)
        print(f'[SM HOLDINGS] Fetched {len(holdings)} token holdings from Nansen (5 credits)')

        if token_address:
            return holdings.get(token_address.lower(), {})
        return holdings

    except Exception as e:
        log_api_error('sm_holdings', str(e), 0)
        return {} if token_address else {}


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
    Disk-cached with 12-hour TTL (10 credits per call -- only paid on cache miss).

    Endpoint: POST /api/v1/tgm/holders
    Returns: {'top5_pct': float, 'concentration_risk': bool}
    concentration_risk = True if top5_pct > 50

    Budget guard: if daily budget is already exceeded, returns safe defaults
    instead of burning 10 credits on a cache miss.
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

    # Cache miss -- check daily budget before burning 10 credits
    try:
        from credit_tracker import check_daily_budget
        budget_ok, budget_msg = check_daily_budget()
        if not budget_ok:
            print(f'[HOLDERS] Budget exceeded ({budget_msg}) -- returning safe defaults for {addr_key[:16]}...')
            return {'top5_pct': 0.0, 'concentration_risk': False}
    except ImportError:
        pass  # credit_tracker not available, proceed with API call

    # Fetch from API (10 credits)
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
    """
    Load SM discovery candidates from disk cache. Returns [] on any error.

    Cache format (v2): {'updated_at': ISO str, 'cached_tokens': [list of token dicts]}
    Backward compat (v1 bare list): returns the list directly.
    """
    try:
        raw = json.loads(_SM_DISCOVERY_CACHE_FILE.read_text())
        if isinstance(raw, list):
            # v1 bare list — treat as fully stale, let caller refetch
            return []
        return raw.get('cached_tokens', [])
    except Exception:
        return []


def _save_sm_discovery_to_disk(candidates: list) -> None:
    """
    Write candidates to disk cache atomically.

    Cache format (v2): {'updated_at': ISO str, 'cached_tokens': [list of token dicts]}
    Each token in cached_tokens also gets its own 'cached_at' timestamp.
    """
    now = datetime.now(timezone.utc).isoformat()
    enriched = []
    for c in candidates:
        entry = dict(c)
        entry['cached_at'] = now
        enriched.append(entry)
    payload = {'updated_at': now, 'cached_tokens': enriched}
    tmp = _SM_DISCOVERY_CACHE_FILE.with_suffix('.tmp')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload))
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
        _enrich_liquidity_pool_resolver(needs_dexscreener)


def _enrich_liquidity_pool_resolver(candidates: list) -> None:
    """
    Fetch liquidity and price from pool_cache.json for candidates with $0 liquidity.
    Uses pool_resolver which reads from the permanent DexScreener cache.
    Mutates candidates in place.
    """
    if not candidates:
        return
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from pool_resolver import get_pool_info, resolve_single
        for c in candidates:
            addr = c['address'].lower()
            info = get_pool_info(addr)
            if not info:
                # Cache miss — last-resort live call (adds to cache)
                info = resolve_single(addr)
            if info:
                if c['liquidity_usd'] == 0:
                    c['liquidity_usd'] = info.get('liquidity_usd', 0) or 0
                if c['price_usd'] == 0:
                    c['price_usd'] = info.get('price_usd', 0) or 0
    except Exception as e:
        print(f'[SM DISCOVERY] Pool resolver enrichment error: {e}')


def discover_sm_from_holdings(chain: str = 'base', limit: int = 20) -> list[dict]:
    """
    Discover tokens from SM wallet holdings (primary SM signal source).

    Calls get_sm_holdings(chain) — already cached by load_candidate_tokens_base()
    at startup, so this function costs 0 extra credits per run.

    Filters: holders_count >= 3 (minimum SM conviction threshold).
    Sorts: holders_count descending (most SM interest first).

    Returns token dicts in discovery format:
      {address, symbol, name, volume_24h, market_cap_usd, pool_address,
       liquidity_usd, price_usd, holders_count, source, trader_count,
       netflow_24h_usd, chain}

    If get_sm_holdings() returns empty (API error or cache miss), returns [].
    """
    if _is_mock_mode():
        # Mock returns a synthetic token so the merge logic gets exercised
        return [{
            'address': '0xmock0000000000000000000000000000000001',
            'symbol': 'MOCK',
            'name': 'Mock Token',
            'volume_24h': 0.0,
            'market_cap_usd': 5_000_000.0,
            'pool_address': '',
            'liquidity_usd': 0.0,
            'price_usd': 1.50,
            'holders_count': 8,
            'source': 'sm_holdings',
            'trader_count': 8,
            'netflow_24h_usd': 0.0,
            'chain': chain,
            'token_age_days': 0,
        }]

    holdings = get_sm_holdings(chain=chain)
    if not holdings:
        return []

    # Filter: minimum 3 SM wallets holding this token
    candidates = []
    for addr, data in holdings.items():
        hc = int(data.get('holders_count', 0) or 0)
        if hc < 3:
            continue
        symbol = data.get('token_symbol', '').upper() or addr[:10]
        if symbol in _SKIP_SYMBOLS:
            continue
        candidates.append({
            'address': addr,
            'symbol': symbol,
            'name': data.get('token_symbol', addr[:10]),
            'volume_24h': 0.0,       # enriched from screener cache below
            'market_cap_usd': 0.0,   # enriched from screener cache below
            'pool_address': '',
            'liquidity_usd': 0.0,
            'price_usd': 0.0,
            'holders_count': hc,
            'source': 'sm_holdings',
            'trader_count': hc,      # holders_count IS the SM conviction signal
            'netflow_24h_usd': 0.0,  # enriched from screener cache below
            'chain': chain,
            'token_age_days': int(data.get('token_age_days', 0) or 0),
        })

    # Sort by holders_count descending — most SM conviction first
    candidates.sort(key=lambda x: x['holders_count'], reverse=True)
    return candidates[:limit]


def load_candidate_tokens_base(config: dict = None) -> list:
    """
    Load Base tokens from three discovery sources, merged and deduplicated.

    Priority order (highest to lowest SM signal quality):
      1. sm_holdings  — tokens SM wallets are holding (primary, best signal)
      2. nansen_sm_netflow  — tokens with recent SM netflow activity (secondary)
      3. screener     — screener netflow fallback (tertiary, free)

    All three sources share the same 4-hour cache TTL.
    Liquidity/volume/price enriched from screener cache (free).

    Also pre-warms the SM holdings cache (get_sm_holdings) so that subsequent
    per-token get_sm_netflow(enrich=True) calls hit disk cache instead of API.

    Returns token dicts in the shape run_pipeline.py expects:
      {address, symbol, name, volume_24h, market_cap_usd, pool_address,
       liquidity_usd, price_usd, holders_count, source, trader_count,
       netflow_24h_usd, chain}

    Falls back to [] if all sources fail (pipeline handles fallback to GeckoTerminal).
    """
    if _is_mock_mode():
        # Mock mode: return sm_holdings discovery so merge is exercised
        return discover_sm_from_holdings(chain='base', limit=20)

    # Pre-warm SM holdings cache: one bulk call (5 credits) feeds all tokens.
    # Subsequent per-token get_sm_netflow(enrich=True) calls hit disk cache.
    get_sm_holdings(chain='base')

    # --- Tier 1: sm_holdings (primary SM signal) ---
    holdings_tokens = discover_sm_from_holdings(chain='base', limit=20)

    # --- Tier 2: nansen_sm_netflow (secondary) ---
    # Check its own disk cache before making an API call
    netflow_tokens = []
    if _sm_discovery_cache_is_fresh():
        cached = _load_sm_discovery_from_disk()
        if cached:
            print(f'[SM DISCOVERY] Netflow cache hit: {len(cached)} candidates (fresh)')
            netflow_tokens = cached
    else:
        # Cache miss — fetch 1 page from Nansen netflow endpoint
        import requests
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
                print(f'[SM DISCOVERY] Netflow Nansen error: {r.status_code} - {r.text[:100]}')
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
                    if mc < 1_000_000:
                        continue
                    netflow_tokens.append({
                        'address': addr,
                        'symbol': sym,
                        'name': sym,
                        'volume_24h': 0.0,
                        'market_cap_usd': mc,
                        'pool_address': '',
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
            print(f'[SM DISCOVERY] Netflow request error: {e}')

        if netflow_tokens:
            _save_sm_discovery_to_disk(netflow_tokens)
            print(f'[SM DISCOVERY] Netflow fetched {len(netflow_tokens)} candidates')

    # --- Merge: holdings primary, netflow secondary, deduplicate by address ---
    # sm_holdings source takes priority — if same address appears in both, drop from netflow
    holdings_addresses = {t['address'] for t in holdings_tokens}
    netflow_tokens = [t for t in netflow_tokens if t['address'] not in holdings_addresses]

    candidates = holdings_tokens + netflow_tokens

    # --- Tier 3: screener fallback (free, no API call) ---
    _enrich_from_screener_cache(candidates)

    # Sort by source priority then holders_count — sm_holdings first, then netflow
    candidates.sort(key=lambda x: (
        0 if x['source'] == 'sm_holdings' else 1,
        x.get('holders_count', 0)
    ), reverse=True)

    print(f'[SM DISCOVERY] Total candidates: {len(candidates)} '
          f'(holdings={len(holdings_tokens)}, netflow={len(netflow_tokens)})')

    # Emit unified log discovery event
    if emit is not None:
        emit("discovery", {
            "total": len(candidates),
            "holdings_count": len(holdings_tokens),
            "netflow_count": len(netflow_tokens),
            "tokens": [
                {
                    "symbol": t["symbol"],
                    "address": t["address"],
                    "source": t.get("source"),
                    "holders_count": t.get("holders_count", 0),
                    "liquidity_usd": t.get("liquidity_usd", 0),
                    "routing_token": t.get("routing_token"),
                }
                for t in candidates
            ],
        }, source="scanner")

    return candidates


# ---------------------------------------------------------------------------
