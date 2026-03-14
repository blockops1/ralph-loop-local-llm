"""
scanner.py — Nansen API wrapper with mock support

Provides functions to fetch token data from Nansen API:
- get_price_ohlcv: OHLCV price data
- get_sm_netflow: Smart money netflows
- get_sm_dex_trades: Smart money DEX trades
- get_top_holders: Top holder concentration
- get_pool_liquidity: Pool liquidity via web3.py
- discover_sm_tokens: Dynamic SM token discovery

Mock mode is active when NANSEN_MOCK=1 OR NANSEN_API_KEY is not set.

AUTH RULES (critical — wrong header = silent 404s):
  header name: 'apikey'  (lowercase — NOT 'apiKey')
  base url:    https://api.nansen.ai/api/v1/
  method:      POST with JSON body (all endpoints)
"""

import os
import random
import time
from typing import Optional


# Symbols to skip (stablecoins, wrapped assets)
_SKIP_SYMBOLS = {"USDC", "USDT", "DAI", "USDS", "WETH", "WBTC", "cbBTC", "cbETH"}


def _is_mock_mode() -> bool:
    """Check if mock mode is active."""
    return os.environ.get('NANSEN_MOCK') == '1' or not os.environ.get('NANSEN_API_KEY')


def _headers() -> dict:
    """Return correct Nansen auth headers. apikey MUST be lowercase."""
    return {
        'apikey': os.environ['NANSEN_API_KEY'],
        'Content-Type': 'application/json',
    }


def get_price_ohlcv(token_address: str, chain: str = 'base', interval: str = '1d', limit: int = 14) -> list[dict]:
    """
    Fetch OHLCV price data for a token.

    Endpoint: POST /api/v1/tgm/token-ohlcv
    Body: {"token_address": str, "chain": str, "interval": str, "limit": int}

    Returns: list of OHLCV dicts — same format as support_resistance.calculate_sr() expects:
        [{'timestamp': int, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}, ...]
    """
    if _is_mock_mode():
        print(f"[MOCK] get_price_ohlcv called for {token_address}")
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
                'open': round(open_price, 6),
                'high': round(high_price, 6),
                'low': round(low_price, 6),
                'close': round(close_price, 6),
                'volume': round(volume, 2)
            })
            current_price = close_price
        return candles

    import requests
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/tgm/token-ohlcv',
            headers=_headers(),
            json={'token_address': token_address, 'chain': chain, 'interval': interval, 'limit': limit},
            timeout=30,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        if e.response and e.response.status_code == 422:
            # No OHLCV data available for this token
            print(f'[SKIP OHLCV] No data for {token_address}')
            return []
        raise
    
    candles = []
    for item in r.json().get('data', []):
        candles.append({
            'timestamp': int(item.get('timestamp', 0)),
            'open':   float(item.get('open',   0)),
            'high':   float(item.get('high',   0)),
            'low':    float(item.get('low',    0)),
            'close':  float(item.get('close',  0)),
            'volume': float(item.get('volume', 0)),
        })
    return candles


def get_sm_netflow(token_address: str, chain: str = 'base', window_hours: int = 24) -> dict:
    """
    Fetch smart money netflow data for a token.

    Endpoint: POST /api/v1/smart-money/netflow
    Body: {"token_address": str, "chain": str, "window": "24h"}

    Returns: {'netflow_usd': float, 'is_positive': bool, 'window_hours': int}
    """
    if _is_mock_mode():
        print(f"[MOCK] get_sm_netflow called for {token_address}")
        return {'netflow_usd': 125000.0, 'is_positive': True, 'window_hours': window_hours}

    import requests
    window_str = f"{window_hours}h"
    r = requests.post(
        'https://api.nansen.ai/api/v1/smart-money/netflow',
        headers=_headers(),
        json={'token_address': token_address, 'chain': chain, 'window': window_str},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get('data', {})
    netflow_usd = float(data.get('netflow_usd', data.get('net_flow_usd', 0)))
    return {
        'netflow_usd': netflow_usd,
        'is_positive': netflow_usd > 0,
        'window_hours': window_hours,
    }


def get_sm_dex_trades(token_address: str, chain: str = 'base', window_hours: int = 24) -> dict:
    """
    Fetch smart money DEX trades data for a token.

    Endpoint: POST /api/v1/tgm/dex-trades
    Body: {"token_address": str, "chain": str}

    Returns: {'sm_trader_count': int, 'sm_buy_volume_usd': float, 'sm_sell_volume_usd': float, 'avg_daily_volume_14d': float}
    """
    if _is_mock_mode():
        print(f"[MOCK] get_sm_dex_trades called for {token_address}")
        return {
            'sm_trader_count': 14,
            'sm_buy_volume_usd': 85000.0,
            'sm_sell_volume_usd': 12000.0,
            'avg_daily_volume_14d': 30000.0,
        }

    import requests
    r = requests.post(
        'https://api.nansen.ai/api/v1/tgm/dex-trades',
        headers=_headers(),
        json={'token_address': token_address, 'chain': chain},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get('data', {})
    # Sum buys/sells from trade list if response is a list
    if isinstance(data, list):
        sm_buys = sum(float(t.get('buy_volume_usd', 0)) for t in data)
        sm_sells = sum(float(t.get('sell_volume_usd', 0)) for t in data)
        sm_count = len(set(t.get('trader_address', '') for t in data))
        avg_vol = (sm_buys + sm_sells) / max(len(data), 1)
    else:
        sm_buys = float(data.get('sm_buy_volume_usd', data.get('buy_volume_usd', 0)))
        sm_sells = float(data.get('sm_sell_volume_usd', data.get('sell_volume_usd', 0)))
        sm_count = int(data.get('sm_trader_count', data.get('trader_count', 0)))
        avg_vol = float(data.get('avg_daily_volume_14d', 0))
    return {
        'sm_trader_count': sm_count,
        'sm_buy_volume_usd': sm_buys,
        'sm_sell_volume_usd': sm_sells,
        'avg_daily_volume_14d': avg_vol,
    }


def get_top_holders(token_address: str, chain: str = 'base', top_n: int = 5) -> dict:
    """
    Fetch top holder concentration data for a token.

    Endpoint: POST /api/v1/tgm/holders
    Body: {"token_address": str, "chain": str}

    Returns: {'top5_pct': float, 'concentration_risk': bool}
    concentration_risk = True if top5_pct > 50
    """
    if _is_mock_mode():
        print(f"[MOCK] get_top_holders called for {token_address}")
        return {'top5_pct': 38.2, 'concentration_risk': False}

    import requests
    r = requests.post(
        'https://api.nansen.ai/api/v1/tgm/holders',
        headers=_headers(),
        json={'token_address': token_address, 'chain': chain},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get('data', [])
    # Sum top N holders' percentage
    if isinstance(data, list):
        top5_pct = sum(float(h.get('percentage', h.get('pct', 0))) for h in data[:top_n])
    else:
        top5_pct = float(data.get('top5_pct', data.get('top_holders_pct', 0)))
    return {
        'top5_pct': top5_pct,
        'concentration_risk': top5_pct > 50,
    }


def get_pool_liquidity(token_address: str, chain: str = 'base') -> dict:
    """
    Fetch pool liquidity data via web3.py call to Aerodrome pool on Base.

    Live call: web3.py → Base RPC https://mainnet.base.org
    Note: requires pool address from tokens.json (not auto-discovered).

    Returns: {'tvl_usd': float, 'meets_minimum': bool}
    meets_minimum = True if tvl_usd >= 100000
    """
    if _is_mock_mode():
        print(f"[MOCK] get_pool_liquidity called for {token_address}")
        return {'tvl_usd': 500000.0, 'meets_minimum': True}

    try:
        from web3 import Web3
    except ImportError:
        print(f"[MOCK] get_pool_liquidity (web3 unavailable) for {token_address}")
        return {'tvl_usd': 500000.0, 'meets_minimum': True}

    try:
        w3 = Web3(Web3.HTTPProvider('https://mainnet.base.org', request_kwargs={'timeout': 15}))
        if not w3.is_connected():
            raise ConnectionError("Base RPC unavailable")

        # Pool address must be provided via tokens.json — no auto-discovery
        # Placeholder: returns mock until pool_address is wired in from config
        # TODO: accept pool_address param and remove this fallback
        return {'tvl_usd': 500000.0, 'meets_minimum': True}

    except Exception as e:
        print(f"[MOCK] get_pool_liquidity fallback ({e}) for {token_address}")
        return {'tvl_usd': 500000.0, 'meets_minimum': True}


def discover_sm_tokens(chain: str = 'base', limit: int = 20) -> list[dict]:
    """
    Discover tokens that smart money wallets are actively holding.

    Purpose: Replaces the static smart_money list in tokens.json. Returns tokens
    that smart money wallets are actively holding on the specified chain, to be
    used as the dynamic scanning universe.

    Endpoint: POST /api/v1/smart-money/holdings
    Body: {"chains": [chain]}

    Returns: list of token dicts with keys:
        - address: token address
        - symbol: token symbol
        - name: token name
        - volume_24h: 24h volume in USD
        - holders_count: number of holders
        - value_usd: total value held by SM wallets in USD

    Filters out stablecoins and wrapped assets (USDC, USDT, DAI, USDS, WETH, WBTC, cbBTC, cbETH).
    Sorted by holders_count descending. Returns at most `limit` tokens.
    """
    if _is_mock_mode():
        print(f"[MOCK] discover_sm_tokens called for chain={chain}, limit={limit}")
        return [
            {"address": "0xb695559b26bb2c9703ef1935c37aeae9526bab07", "symbol": "MOLT", "name": "Moltbook", "volume_24h": 3732176.0, "holders_count": 18, "value_usd": 512000.0},
            {"address": "0x22af33fe49fd1fa80c7149773dde5890d3c76f3b", "symbol": "BNKR", "name": "BankrCoin", "volume_24h": 980000.0, "holders_count": 24, "value_usd": 890000.0},
            {"address": "0x9f86db9fc6f7c9408e8fda3ff8ce4e78ac7a6b07", "symbol": "CLAWD", "name": "clawd.atg.eth", "volume_24h": 420000.0, "holders_count": 11, "value_usd": 310000.0},
            {"address": "0xf30bf00edd0c22db54c9274b90d2a4c21fc09b07", "symbol": "FELIX", "name": "FELIX", "volume_24h": 195000.0, "holders_count": 9, "value_usd": 180000.0},
            {"address": "0x4e6c9f48f73e54ee5f3ab7e2992b2d733d0d0b07", "symbol": "JUNO", "name": "Juno Agent", "volume_24h": 120000.0, "holders_count": 7, "value_usd": 95000.0},
        ]

    try:
        import requests
        r = requests.post(
            'https://api.nansen.ai/api/v1/smart-money/holdings',
            headers=_headers(),
            json={'chains': [chain]},
            timeout=30,
        )
        r.raise_for_status()
        response = r.json()

        # Adapt to response structure — check for 'data' or 'tokens' key
        raw_tokens = response.get('data', response.get('tokens', []))

        tokens = []
        for item in raw_tokens:
            # Skip stablecoins and wrapped assets
            symbol = item.get('token_symbol', item.get('symbol', '')).upper()
            if symbol in _SKIP_SYMBOLS:
                continue

            tokens.append({
                'address': item.get('token_address', item.get('address', '')),
                'symbol': item.get('token_symbol', item.get('symbol', '')),
                'name': item.get('token_name', item.get('name', '')),
                'volume_24h': float(item.get('volume_24h', 0.0)),
                'holders_count': int(item.get('holders_count', 0)),
                'value_usd': float(item.get('value_usd', 0.0)),
            })

        # Sort by holders_count descending
        tokens.sort(key=lambda t: t['holders_count'], reverse=True)

        return tokens[:limit]

    except Exception as e:
        print(f"[ERROR] discover_sm_tokens failed: {e}")
        return []
