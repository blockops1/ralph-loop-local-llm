"""Smart money flow scoring and direction logic for HyperLiquid tokens."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import os
import time
import requests


def get_nansen_flow(symbols: list, config: dict) -> dict:
    """
    Fetch Nansen smart money flow data for a list of symbols.
    
    Args:
        symbols: List of token symbols to fetch data for
        config: Configuration dict
        
    Returns:
        Dict mapping symbol to flow data with keys:
        buy_pressure_usd, sell_pressure_usd, net_flow_usd, 
        trader_count, smart_money_net_flow_usd
    """
    api_key = os.environ.get("NANSEN_API_KEY")

    cache_path = Path(__file__).parent.parent / "data" / "nansen_flow_cache.json"
    cache_ttl = config.get("nansen_cache_ttl", 3600)  # Default 1 hour

    # Try to load from cache first (regardless of whether API key is set)
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                cache_data = json.load(f)

            cache_time = cache_data.get("timestamp", 0)
            if time.time() - cache_time < cache_ttl:
                cached_symbols = cache_data.get("symbols", {})
                result = {}
                for symbol in symbols:
                    if symbol in cached_symbols:
                        result[symbol] = cached_symbols[symbol]
                if result:
                    return result
        except (json.JSONDecodeError, IOError):
            pass

    # No API key — can't fetch live data
    if not api_key:
        return {}

    # Fetch from API
    url = "https://api.nansen.ai/api/v1/tgm/token-flow"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        result = {}
        for symbol in symbols:
            # Find token data in response
            token_data = None
            if isinstance(data, list):
                for item in data:
                    if item.get("symbol") == symbol:
                        token_data = item
                        break
            elif isinstance(data, dict) and "data" in data:
                for item in data["data"]:
                    if item.get("symbol") == symbol:
                        token_data = item
                        break
            
            if token_data:
                result[symbol] = {
                    "buy_pressure_usd": float(token_data.get("buyPressureUsd", 0)),
                    "sell_pressure_usd": float(token_data.get("sellPressureUsd", 0)),
                    "net_flow_usd": float(token_data.get("netFlowUsd", 0)),
                    "trader_count": int(token_data.get("traderCount", 0)),
                    "smart_money_net_flow_usd": float(token_data.get("smartMoneyNetFlowUsd", 0))
                }
        
        # Cache results
        cache_dir = cache_path.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        cache_data = {
            "timestamp": time.time(),
            "symbols": result
        }
        
        with open(cache_path, "w") as f:
            json.dump(cache_data, f, indent=2)
        
        return result
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        return {}


def score_token(token: dict, flow: dict, config: dict) -> dict:
    """
    Score a single token based on Nansen smart money data.
    
    Args:
        token: Token dict with price_change_24h_pct, funding_rate, etc.
        flow: Flow data dict from get_nansen_flow
        config: Configuration dict
        
    Returns:
        Dict with keys: symbol, score, direction, components
    """
    symbol = token.get("name")
    
    # Get flow data for this token
    flow_data = flow.get(symbol, {})
    
    buy_pressure = flow_data.get("buy_pressure_usd", 0)
    sell_pressure = flow_data.get("sell_pressure_usd", 0)
    trader_count = flow_data.get("trader_count", 0)
    smart_money_net_flow = flow_data.get("smart_money_net_flow_usd", 0)
    
    # Component 1: buy_pressure_score (0-25)
    total_pressure = buy_pressure + sell_pressure
    if total_pressure > 0:
        buy_ratio = buy_pressure / total_pressure
    else:
        buy_ratio = 0.5  # Neutral if no data
    buy_pressure_score = min(25, int(buy_ratio * 25))
    
    # Component 2: momentum_score (0-25)
    # Map price_change_24h_pct to 0-25 scale
    # Range: -10% to +10% maps to 0-25
    price_change = token.get("price_change_24h_pct", 0)
    momentum_score = min(25, max(0, int((price_change + 10) / 20 * 25)))
    
    # Component 3: trader_count_score (0-25)
    # Cap at 1000 traders for full score
    trader_count_score = min(25, int(trader_count / 1000 * 25))
    
    # Component 4: smart_money_score (0-25)
    # Weighted 2x but capped at 25
    # Map smart money flow: -100k to +100k maps to 0-25
    smart_money_score = min(25, max(0, int((smart_money_net_flow + 100000) / 200000 * 25)))
    
    # Total score
    total_score = buy_pressure_score + momentum_score + trader_count_score + smart_money_score
    
    # Determine direction
    direction = determine_direction(token, flow_data)
    
    return {
        "symbol": symbol,
        "score": total_score,
        "direction": direction,
        "components": {
            "buy_pressure_score": buy_pressure_score,
            "momentum_score": momentum_score,
            "trader_count_score": trader_count_score,
            "smart_money_score": smart_money_score
        }
    }


def determine_direction(token: dict, flow: dict) -> str:
    """
    Determine long/short direction based on token and flow data.
    
    Args:
        token: Token dict with price_change_24h_pct, funding_rate, etc.
        flow: Flow data dict for this token
        
    Returns:
        'long' or 'short'
    """
    buy_pressure = flow.get("buy_pressure_usd", 0)
    sell_pressure = flow.get("sell_pressure_usd", 0)
    smart_money_net_flow = flow.get("smart_money_net_flow_usd", 0)
    
    # Default is long
    direction = "long"
    
    # Condition a: net selling
    if buy_pressure < sell_pressure:
        direction = "short"
    
    # Condition b: crowded long reversal
    funding_rate = token.get("funding_rate", 0)
    price_change = token.get("price_change_24h_pct", 0)
    if funding_rate > 0.0001 and price_change < -2.0:
        direction = "short"
    
    # Condition c: strong SM short with weak buying
    if smart_money_net_flow < -5000 and buy_pressure < 500000:
        direction = "short"
    
    return direction


def score_candidates(candidates: list, config: dict) -> list:
    """
    Score all candidate tokens and return sorted list.
    
    Args:
        candidates: List of token dicts from scanner
        config: Configuration dict
        
    Returns:
        List of scored dicts sorted by score descending, filtered by threshold
    """
    symbols = [token.get("name") for token in candidates]
    
    # Get Nansen flow data for all symbols
    flow_data = get_nansen_flow(symbols, config)
    
    # Score each token
    scored_tokens = []
    for token in candidates:
        score_result = score_token(token, flow_data, config)
        score_result.update(token)  # Add original token fields
        scored_tokens.append(score_result)
    
    # Filter by threshold and sort
    threshold = config.get("entry_score_threshold", 60)
    filtered = [t for t in scored_tokens if t["score"] >= threshold]
    filtered.sort(key=lambda x: x["score"], reverse=True)
    
    return filtered


if __name__ == "__main__":
    from scanner import load_config, scan_universe
    
    config = load_config()
    candidates = scan_universe(config)
    
    print(f"Scanning {len(candidates)} candidates...")
    scored = score_candidates(candidates, config)
    
    print(f"\nTop {len(scored)} scored tokens (score >= {config['entry_score_threshold']}):")
    for token in scored[:10]:
        print(f"  {token['symbol']}: score={token['score']}, direction={token['direction']}")
        print(f"    Components: {token['components']}")
