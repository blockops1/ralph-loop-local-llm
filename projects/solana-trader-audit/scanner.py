import json, os, time, requests
from pathlib import Path

FLOW_CACHE_FILE = Path(__file__).parent.parent / 'data' / 'flow_intel_cache.json'
FLOW_CACHE_TTL = 1800  # 30 min
# Discovery cache: shared across all runs, refreshed every 1 hour
DISCOVERY_CACHE_FILE = Path(__file__).parent.parent / 'data' / 'solana_discovery_cache.json'
DISCOVERY_CACHE_TTL = 3600  # 1 hour
WSOL_ADDRESS = 'So11111111111111111111111111111111111111112'
NANSEN_BASE = 'https://api.nansen.ai'


def _headers():
    return {'apikey': os.environ['NANSEN_API_KEY'], 'Content-Type': 'application/json'}


def get_sol_price_usd() -> float:
    """Fetch SOL/USD from DexScreener using WSOL address. Fallback: 130.0."""
    try:
        r = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{WSOL_ADDRESS}',
            headers={'Accept': 'application/json'}, timeout=10)
        if r.status_code == 200:
            pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == 'solana']
            if pairs:
                best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
                price = float(best.get('priceUsd', 0) or 0)
                if price > 0:
                    return price
    except Exception:
        pass
    return 130.0


def get_btc_5h_change() -> float:
    """Get BTC ~5h price change (last 5 hourly data points). Returns 0.0 on error."""
    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
            params={'vs_currency': 'usd', 'days': '1', 'interval': 'hourly'},
            timeout=10)
        if r.status_code == 200:
            prices = r.json().get('prices', [])
            if len(prices) >= 5:
                old = prices[-5][1]  # oldest of last 5 hourly data points
                new = prices[-1][1]
                return (new - old) / old * 100
    except Exception:
        pass
    return 0.0


def _enrich_liquidity(candidates: list) -> None:
    """Fetch liquidity from DexScreener for all candidates in a single batch request."""
    if not candidates:
        return
    try:
        addrs = [c['address'] for c in candidates]
        # Batch: DexScreener accepts comma-separated addresses
        addr_str = ','.join(addrs)
        r = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{addr_str}',
            headers={'Accept': 'application/json'},
            timeout=15
        )
        if r.status_code != 200:
            print(f'[SCANNER] DexScreener liquidity fetch failed: {r.status_code}')
            return
        pairs_data = r.json()
        if isinstance(pairs_data, dict):
            pairs_data = pairs_data.get('pairs', [])
        # Build lookup: address -> liquidity
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
            c['liquidity_usd'] = liq_map.get(addr, 0)
            if c.get('price_usd', 0) == 0:
                c['price_usd'] = price_map.get(addr, 0)
    except Exception as e:
        print(f'[SCANNER] liquidity enrichment error: {e}')


def _discovery_cache_is_fresh() -> bool:
    """Return True if discovery cache exists and is younger than TTL."""
    if not DISCOVERY_CACHE_FILE.exists():
        return False
    age = time.time() - DISCOVERY_CACHE_FILE.stat().st_mtime
    return age < DISCOVERY_CACHE_TTL


def _load_discovery_from_disk() -> list:
    """Load candidates from disk cache. Returns [] on any error."""
    try:
        return json.loads(DISCOVERY_CACHE_FILE.read_text())
    except Exception:
        return []


def _save_discovery_to_disk(candidates: list) -> None:
    """Write candidates to disk cache atomically."""
    tmp = DISCOVERY_CACHE_FILE.with_suffix('.tmp')
    DISCOVERY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(candidates))
    tmp.replace(DISCOVERY_CACHE_FILE)


def load_candidate_tokens(config: dict) -> list:
    """Load Solana tokens from Nansen smart-money/netflow API.
    Returns top tokens by smart trader activity, filtered by quality criteria.

    Discovery data is cached to disk for 4 hours (1 API call per 4h, not per run).
    Only 1 page is fetched per cache refresh (~50 tokens) — sufficient given SM ranking.
    """
    # Check disk cache first
    if _discovery_cache_is_fresh():
        cached = _load_discovery_from_disk()
        if cached:
            print(f'[SCANNER] Discovery cache hit: {len(cached)} tokens (fresh)')
            return cached

    # Cache miss — fetch 1 page from Nansen (5 credits)
    stablecoins = set(s.upper() for s in config.get('stablecoin_symbols', []))
    min_liq = config.get('min_liquidity_usd', 100000)
    min_mc = config.get('min_market_cap_usd', 5000000)
    max_mc = config.get('max_market_cap_usd', 500000000)
    min_age = config.get('min_token_age_days', 30)

    candidates = []
    seen = set()

    try:
        r = requests.post(
            f'{NANSEN_BASE}/api/v1/smart-money/netflow',
            headers=_headers(),
            json={
                'chains': ['solana'],
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
            print(f'[SCANNER] Nansen netflow error: {r.status_code} - {r.text[:100]}')
        else:
            data = r.json()
            items = data.get('data', [])
            for item in items:
                addr = (item.get('token_address') or '').lower()
                sym = (item.get('token_symbol') or '').upper()
                if not addr or not sym or sym in seen:
                    continue
                seen.add(sym)
                if sym in stablecoins:
                    continue
                mc = item.get('market_cap_usd', 0) or 0
                age = item.get('token_age_days', 0) or 0
                trader_count = item.get('trader_count', 0) or 0
                if mc < min_mc or mc > max_mc:
                    continue
                if age < min_age:
                    continue
                candidates.append({
                    'symbol': sym,
                    'address': addr,
                    'chain': 'solana',
                    'market_cap_usd': mc,
                    'token_age_days': age,
                    'trader_count': trader_count,
                    'netflow_24h_usd': item.get('net_flow_24h_usd', 0) or 0,
                    'price_usd': item.get('price_usd', 0),
                    'volume_24h_usd': item.get('volume_24h_usd', 0) or 0,
                })
    except Exception as e:
        print(f'[SCANNER] Nansen netflow request error: {e}')

    # Fetch liquidity from DexScreener (free, batched)
    _enrich_liquidity(candidates)

    # Filter by liquidity
    candidates = [c for c in candidates if c.get('liquidity_usd', 0) >= min_liq]

    # Sort by trader_count descending
    candidates.sort(key=lambda x: x['trader_count'], reverse=True)

    # Save to disk cache
    _save_discovery_to_disk(candidates)
    print(f'[SCANNER] Discovery cache miss: fetched {len(candidates)} candidates, cached for 4h')

    return candidates


def get_flow_intelligence(token_address: str) -> dict:
    """Fetch flow intelligence for a token from Nansen smart-money/netflow. Cached 30 min."""
    cache = {}
    if FLOW_CACHE_FILE.exists():
        try:
            cache = json.loads(FLOW_CACHE_FILE.read_text())
        except Exception:
            cache = {}
    addr = token_address.lower()
    entry = cache.get(addr, {})
    if entry and time.time() - entry.get('ts', 0) < FLOW_CACHE_TTL:
        return entry.get('data', {})

    # Fetch fresh from correct Nansen endpoint
    try:
        r = requests.post(
            f'{NANSEN_BASE}/api/v1/smart-money/netflow',
            headers=_headers(),
            json={
                'chains': ['solana'],
                'filters': {
                    'token_address': token_address,
                    'include_smart_money_labels': ['Smart Trader'],
                },
                'pagination': {'page': 1, 'per_page': 1}
            },
            timeout=30
        )
        if r.status_code == 200:
            items = r.json().get('data', [])
            data = items[0] if items else {}
            cache[addr] = {'ts': time.time(), 'data': data}
            FLOW_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            FLOW_CACHE_FILE.write_text(json.dumps(cache))
            return data
        else:
            print(f'[SCANNER] netflow error for {token_address}: {r.status_code} - {r.text[:100]}')
    except Exception as e:
        print(f'[SCANNER] netflow request error for {token_address}: {e}')
    return {}
