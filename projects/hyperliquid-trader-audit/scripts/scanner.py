"""Market universe scanning and filtering for HyperLiquid perpetuals."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import time
import yaml
from hl_client import get_info_client, get_meta_and_asset_ctxs, load_config


def scan_universe(config: dict) -> list[dict]:
    """
    Scan the HyperLiquid perpetuals universe and filter for tradeable mid-cap tokens.

    Args:
        config: Configuration dict with filtering parameters

    Returns:
        List of filtered token dicts sorted by 24h volume descending
    """
    min_oi_usd = config.get("min_oi_usd", 5_000_000)
    max_oi_usd = config.get("max_oi_usd", 2_000_000_000)
    min_volume_24h_usd = config.get("min_volume_24h_usd", 1_000_000)
    stablecoin_exclusion_list = config.get("stablecoin_exclusion_list", [])
    top_n = config.get("top_n", 20)

    meta, asset_ctxs = get_meta_and_asset_ctxs()
    universe = meta.get("universe", [])  # list of {name, szDecimals, maxLeverage, ...}

    candidates = []
    for idx, (asset_info, ctx) in enumerate(zip(universe, asset_ctxs)):
        name = asset_info.get("name", "")

        # Skip stablecoins
        if name in stablecoin_exclusion_list:
            continue

        # Get mark price
        try:
            mark_price = float(ctx.get("markPx") or 0)
        except (TypeError, ValueError):
            continue
        if mark_price <= 0:
            continue

        # Day notional volume in USD
        try:
            day_volume_usd = float(ctx.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            day_volume_usd = 0.0

        # Open interest in USD = openInterest (in contracts) * markPx
        try:
            oi_contracts = float(ctx.get("openInterest") or 0)
            open_interest_usd = oi_contracts * mark_price
        except (TypeError, ValueError):
            open_interest_usd = 0.0

        # Filter by OI range
        if open_interest_usd < min_oi_usd or open_interest_usd > max_oi_usd:
            continue

        # Filter by volume
        if day_volume_usd < min_volume_24h_usd:
            continue

        # Funding rate
        try:
            funding_rate = float(ctx.get("funding") or 0)
        except (TypeError, ValueError):
            funding_rate = 0.0

        # Prev day price + change
        try:
            prev_day_px = float(ctx.get("prevDayPx") or mark_price)
            price_change_24h_pct = ((mark_price - prev_day_px) / prev_day_px * 100) if prev_day_px else 0.0
        except (TypeError, ValueError):
            prev_day_px = mark_price
            price_change_24h_pct = 0.0

        candidates.append({
            "name": name,
            "asset_index": idx,
            "mark_price": mark_price,
            "funding_rate": funding_rate,
            "open_interest_usd": open_interest_usd,
            "day_volume_usd": day_volume_usd,
            "prev_day_price": prev_day_px,
            "price_change_24h_pct": price_change_24h_pct,
            "max_leverage": asset_info.get("maxLeverage", 1),
        })

    # Sort by 24h volume descending, return top N
    candidates.sort(key=lambda x: x["day_volume_usd"], reverse=True)
    return candidates[:top_n]


def get_btc_4h_change() -> float:
    """Get BTC price change vs yesterday's close (approx 24h, best available without candles)."""
    meta, asset_ctxs = get_meta_and_asset_ctxs()
    universe = meta.get("universe", [])

    for asset_info, ctx in zip(universe, asset_ctxs):
        if asset_info.get("name") == "BTC":
            try:
                mark_px = float(ctx.get("markPx") or 0)
                prev_day_px = float(ctx.get("prevDayPx") or mark_px)
                if prev_day_px == 0:
                    return 0.0
                return (mark_px - prev_day_px) / prev_day_px * 100
            except (TypeError, ValueError):
                return 0.0

    return 0.0  # BTC not found — don't block trading


def cache_results(results: list, cache_path: str = "data/scanner_cache.json"):
    """Cache scan results to JSON file with timestamp."""
    cache_dir = Path(cache_path).parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump({"timestamp": time.time(), "results": results}, f, indent=2)


def load_cached_results(cache_path: str = "data/scanner_cache.json") -> dict:
    """Load cached scan results from JSON file."""
    cache_file = Path(cache_path)
    if not cache_file.exists():
        return {"timestamp": None, "results": []}
    with open(cache_file) as f:
        return json.load(f)


if __name__ == "__main__":
    config = load_config()
    results = scan_universe(config)
    cache_results(results)
    print(f"Found {len(results)} tradeable tokens")
    for token in results[:5]:
        print(f"  {token['name']}: OI=${token['open_interest_usd']:,.0f}, Vol=${token['day_volume_usd']:,.0f}")
