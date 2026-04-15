#!/usr/bin/env python3
"""
multi_pair_scanner.py — Phase 4: Multi-Pair Scanner with v1 + v2 parallel signals

Scans 5 trading pairs using:
  v1: support_resistance.py (swing-point S/R, existing)
  v2: range_engine.py (BB+ATR+ADX, new)

Both signals are logged for comparison. Pairs:
  WETH/USDC, AERO/USDC, ZEN/WETH, cbBTC/WETH, VFY/USDC

Data: CoinGecko via range_data.py (1h + 4h candles)
Liquidity filter: skip if TVL < per-pair threshold (from range_engine_config min_width)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add production apps to path so we can import the modules
PRODUCTION_APPS = Path("/Users/jill/production_apps")
RT_ENGINE_DIR = PRODUCTION_APPS / "rt-range-engine"
RANGE_TRADER_DIR = PRODUCTION_APPS / "range-trader" / "scripts"

sys.path.insert(0, str(RT_ENGINE_DIR))
sys.path.insert(0, str(RANGE_TRADER_DIR))

# Import data layer
from range_data import get_candles_1h, get_candles_4h, PAIRS

# Import v2 engine
from range_engine import get_range as v2_get_range
from range_engine_config import PER_PAIR_CONFIG, Regime, Signal

# Import v1 (swing-point S/R) — use snapshot in rt-range-engine
from support_resistance_v1 import (
    calculate_sr_with_fallback as v1_calculate_sr,
    range_quality_score as v1_range_quality_score,
)


# =============================================================================
# Liquidity Filter
# =============================================================================

# Per-pair TVL thresholds (USD) — derived from range_engine_config min_width
# Wider min_width pairs need more liquid pools to be tradeable
MIN_TVL_USD = {
    "WETH/USDC":  50_000,
    "AERO/USDC":  30_000,
    "ZEN/WETH":   20_000,
    "cbBTC/WETH": 50_000,
    "VFY/USDC":   10_000,
}

# GeckoTerminal rate limit state
_GT_LAST_CALL = 0.0
_GT_MIN_INTERVAL = 12.0


def _gt_throttle():
    """Throttle GeckoTerminal API calls."""
    global _GT_LAST_CALL
    import time as _time
    elapsed = _time.time() - _GT_LAST_CALL
    if elapsed < _GT_MIN_INTERVAL:
        _time.sleep(_GT_MIN_INTERVAL - elapsed)
    _GT_LAST_CALL = _time.time()


def get_pool_tvl(pair: str) -> float:
    """
    Get current TVL for a pair from GeckoTerminal pool data.
    Returns 0.0 on error or if pool not found.
    Retries up to 2 times on transient network errors.
    """
    pair_config = PAIRS.get(pair)
    if not pair_config:
        return 0.0

    pool_address = pair_config.get("pool", "")
    if not pool_address:
        return 0.0

    last_error = None
    for attempt in range(3):
        try:
            import requests

            _gt_throttle()
            resp = requests.get(
                f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_address}",
                headers={"Accept": "application/json;version=20230302"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            attrs = data.get("data", {}).get("attributes", {})
            tvl = float(attrs.get("reserve_in_usd", 0) or 0)
            print(f"[TVL] {pair}: ${tvl:,.0f}")
            return tvl
        except Exception as e:
            last_error = e
            if attempt < 2:
                wait = 5 * (2 ** attempt)
                print(f"[TVL] {pair}: retry {attempt+1}/3 in {wait}s — {e}")
                import time as _time
                _time.sleep(wait)
            else:
                print(f"[TVL] {pair}: failed after 3 attempts — {e}")
                return 0.0
    return 0.0


# =============================================================================
# V1 Signal Formatter
# =============================================================================

def format_v1_signal(pair: str, sr: dict, score: int, price_usd: float) -> dict:
    """Format v1 (support_resistance) output into a signal dict."""
    if sr is None:
        return {
            "engine": "v1_support_resistance",
            "pair": pair,
            "signal": "NO_SIGNAL",
            "regime": "UNKNOWN",
            "quality_score": 0,
            "support": 0.0,
            "resistance": 0.0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "reason": "No S/R found",
        }

    support = sr.get("support", 0)
    resistance = sr.get("resistance", 0)
    near_support = sr.get("near_support", False)
    near_resistance = sr.get("near_resistance", False)

    # Determine signal
    if near_support and score >= 50:
        signal = "ENTER_LONG"
        entry = support * 1.005
        stop = support * 0.97
        target = resistance * 0.995
        reason = f"Near support (S={support:.4f}), score={score}"
    elif near_resistance:
        signal = "EXIT_LONG"
        entry = 0.0
        stop = 0.0
        target = resistance * 0.995
        reason = f"Near resistance (R={resistance:.4f})"
    else:
        signal = "HOLD"
        entry = support * 1.005
        stop = support * 0.97
        target = resistance * 0.995
        reason = f"Ranging: S={support:.4f} R={resistance:.4f} score={score}"

    return {
        "engine": "v1_support_resistance",
        "pair": pair,
        "signal": signal,
        "regime": "RANGING",  # v1 doesn't have regime detection
        "quality_score": score,
        "support": support,
        "resistance": resistance,
        "entry_price": entry,
        "stop_loss": stop,
        "target": target,
        "current_price": price_usd,
        "reason": reason,
    }


# =============================================================================
# V2 Signal Formatter
# =============================================================================

def format_v2_signal(v2_result: dict) -> dict:
    """Format v2 (range_engine) output into a standard signal dict."""
    return {
        "engine": "v2_range_engine",
        "pair": v2_result.get("pair", ""),
        "signal": v2_result.get("signal", Signal.SKIP),
        "regime": v2_result.get("regime", Regime.TRENDING),
        "quality_score": v2_result.get("quality_score", 0.0),
        "support": v2_result.get("bb", {}).get("lower", 0.0),  # BB lower = support
        "resistance": v2_result.get("bb", {}).get("upper", 0.0),  # BB upper = resistance
        "bb_mid": v2_result.get("bb", {}).get("mid", 0.0),
        "entry_price": v2_result.get("entry_price", 0.0),
        "stop_loss": v2_result.get("stop_loss", 0.0),
        "target": v2_result.get("target", 0.0),
        "atr": v2_result.get("atr", 0.0),
        "adx": v2_result.get("adx", 0.0),
        "rsi": v2_result.get("rsi", 50.0),
        "bandwidth_pct": v2_result.get("bb", {}).get("bandwidth_pct", 0.0),
        "min_width_met": v2_result.get("min_width_met", False),
        "reason": v2_result.get("reason", ""),
    }


# =============================================================================
# Main Scanner
# =============================================================================

def scan_pair(pair: str) -> dict:
    """
    Scan a single pair with both v1 and v2 engines.
    Returns dict with both signals and metadata.
    """
    result = {
        "pair": pair,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "v1": None,
        "v2": None,
        "tvl_usd": 0.0,
        "error": None,
    }

    # Check liquidity
    tvl = get_pool_tvl(pair)
    result["tvl_usd"] = tvl
    min_tvl = MIN_TVL_USD.get(pair, 25_000)
    if tvl < min_tvl:
        result["error"] = f"TVL ${tvl:,.0f} < ${min_tvl:,} minimum"
        result["v1"] = format_v1_signal(pair, None, 0, 0.0)
        result["v2"] = format_v2_signal({
            "pair": pair,
            "signal": Signal.SKIP,
            "regime": Regime.TRENDING,
            "quality_score": 0.0,
            "bb": {"upper": 0.0, "mid": 0.0, "lower": 0.0, "bandwidth_pct": 0.0},
            "atr": 0.0,
            "adx": 0.0,
            "rsi": 50.0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "min_width_met": False,
            "reason": result["error"],
        })
        return result

    # Fetch candles
    try:
        candles_1h = get_candles_1h(pair, days=7)
        candles_4h = get_candles_4h(pair, days=7)
    except Exception as e:
        result["error"] = f"Data fetch failed: {e}"
        result["v1"] = format_v1_signal(pair, None, 0, 0.0)
        result["v2"] = format_v2_signal({
            "pair": pair,
            "signal": Signal.SKIP,
            "regime": Regime.TRENDING,
            "quality_score": 0.0,
            "bb": {"upper": 0.0, "mid": 0.0, "lower": 0.0, "bandwidth_pct": 0.0},
            "atr": 0.0,
            "adx": 0.0,
            "rsi": 50.0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "min_width_met": False,
            "reason": result["error"],
        })
        return result

    if len(candles_1h) < 20:
        result["error"] = f"Insufficient 1h candles: {len(candles_1h)}"
        result["v1"] = format_v1_signal(pair, None, 0, 0.0)
        result["v2"] = format_v2_signal({
            "pair": pair,
            "signal": Signal.SKIP,
            "regime": Regime.TRENDING,
            "quality_score": 0.0,
            "bb": {"upper": 0.0, "mid": 0.0, "lower": 0.0, "bandwidth_pct": 0.0},
            "atr": 0.0,
            "adx": 0.0,
            "rsi": 50.0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "target": 0.0,
            "min_width_met": False,
            "reason": result["error"],
        })
        return result

    # Current price
    price_usd = candles_1h[-1]["close"] if candles_1h else 0.0

    # ---- V1: Support/Resistance ----
    sr = v1_calculate_sr(candles_1h)
    v1_score = v1_range_quality_score(candles_1h, sr) if sr else 0
    result["v1"] = format_v1_signal(pair, sr, v1_score, price_usd)

    # ---- V2: Range Engine (BB+ATR+ADX) ----
    v2_result = v2_get_range(pair, candles_1h, candles_4h)
    result["v2"] = format_v2_signal(v2_result)

    return result


def scan_all() -> list[dict]:
    """
    Scan all 5 pairs. Returns list of scan result dicts.
    """
    pairs = list(PAIRS.keys())
    results = []

    print(f"\n{'='*70}")
    print(f"MULTI-PAIR SCANNER — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Pairs: {', '.join(pairs)}")
    print(f"{'='*70}\n")

    for pair in pairs:
        print(f"[SCAN] {pair}...")
        try:
            r = scan_pair(pair)
            results.append(r)
        except Exception as e:
            print(f"[SCAN] {pair}: ERROR — {e}")
            results.append({
                "pair": pair,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "v1": None,
                "v2": None,
                "tvl_usd": 0.0,
                "error": str(e),
            })

    return results


# =============================================================================
# Output Formatters
# =============================================================================

def print_results(results: list[dict]):
    """Print a formatted table of scan results."""
    print(f"\n{'='*70}")
    print("SCAN RESULTS")
    print(f"{'='*70}\n")

    for r in results:
        pair = r["pair"]
        print(f"\n--- {pair} ---")
        print(f"  TVL: ${r['tvl_usd']:>12,.0f}  |  Error: {r.get('error', 'None')}")

        v1 = r.get("v1")
        v2 = r.get("v2")

        if v1:
            print(f"  V1 | signal={v1['signal']:<12} | regime={v1['regime']:<10} | "
                  f"quality={v1['quality_score']:>5} | "
                  f"S={v1['support']:.4f} R={v1['resistance']:.4f}")
            print(f"      | reason: {v1['reason'][:60]}")

        if v2:
            sig = v2["signal"]
            regime = v2["regime"]
            qs = v2["quality_score"]
            bb_lo = v2.get("support", 0)
            bb_hi = v2.get("resistance", 0)
            bw = v2.get("bandwidth_pct", 0)
            adx = v2.get("adx", 0)
            rsi = v2.get("rsi", 0)
            mw = v2.get("min_width_met", False)
            print(f"  V2 | signal={sig:<12} | regime={regime:<10} | "
                  f"quality={qs:>5.1f} | "
                  f"BB_lo={bb_lo:.4f} BB_hi={bb_hi:.4f} BW={bw:.2f}%")
            print(f"      | ADX={adx:>5.1f} RSI={rsi:>5.1f} | min_width_met={mw}")
            print(f"      | reason: {v2.get('reason', '')[:60]}")


def print_signal_summary(results: list[dict]):
    """Print a compact one-line-per-pair summary of both signals."""
    print(f"\n{'='*70}")
    print("SIGNAL SUMMARY")
    print(f"{'='*70}")
    print(f"{'Pair':<12} {'V1 Signal':<12} {'V2 Signal':<12} {'V2 Regime':<10} "
          f"{'V2 ADX':>6} {'V2 RSI':>6} {'V2 Quality':>9} {'TVL':>12}")
    print("-" * 90)

    for r in results:
        pair = r["pair"]
        v1_sig = r.get("v1", {}).get("signal", "ERROR") or "ERROR"
        v2_sig = r.get("v2", {}).get("signal", "ERROR") or "ERROR"
        v2_reg = r.get("v2", {}).get("regime", "-") or "-"
        v2_adx = r.get("v2", {}).get("adx", 0) or 0
        v2_rsi = r.get("v2", {}).get("rsi", 0) or 0
        v2_qs = r.get("v2", {}).get("quality_score", 0) or 0
        tvl = r.get("tvl_usd", 0) or 0

        print(f"{pair:<12} {v1_sig:<12} {v2_sig:<12} {v2_reg:<10} "
              f"{v2_adx:>6.1f} {v2_rsi:>6.1f} {v2_qs:>9.1f} ${tvl:>10,.0f}")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    results = scan_all()
    print_results(results)
    print_signal_summary(results)

    # Exit code: 0 if no errors, 1 if any pair had an error
    has_errors = any(r.get("error") is not None for r in results)
    sys.exit(1 if has_errors else 0)
