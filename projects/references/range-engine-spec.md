# Range Engine v2 — Technical Specification

**Version:** 1.0
**Date:** 2026-04-15
**Status:** Draft — pending backtest validation

---

## Overview

The range engine replaces the simple swing-point average in `support_resistance.py` with a hybrid **Bollinger Bands + ATR + ADX** system that:

1. Defines range boundaries using statistical bands rather than recent highs/lows
2. Filters out trending markets where mean-reversion strategies fail
3. Sizes positions based on cost-adjusted minimum width per pair
4. Generates specific entry, exit, and stop-loss signals

---

## Indicator Specifications

### 1. Bollinger Bands (BB)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Period | 20 | Standard; balances responsiveness vs. noise |
| Standard deviation multiplier | 2.0 | Captures ~95% of price distribution |
| Moving average type | SMA | Simple, no lag adjustment needed |

**Formula:**
```
mid     = SMA(close, period=20)
std_dev = STDDEV(close, period=20)
upper   = mid + (2.0 × std_dev)
lower   = mid - (2.0 × std_dev)
```

**Interpretation:**
- Price touching upper BB → overextended upward (potential sell zone)
- Price touching lower BB → oversold (potential buy zone)
- BB width (bandwidth) indicates volatility regime

### 2. ATR (Average True Range)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Period | 14 | Standard; captures a full trading cycle |
| Multiplier for stop | 1.5 | Tight enough for meaningful stops, not whipsawed |

**True Range (per bar):**
```
tr = max(high - low, |high - close_prev|, |low - close_prev|)
```

**ATR:**
```
atr = SMA(tr, period=14)
```

**Use:** Trailing stop loss = entry_price - (1.5 × ATR)

### 3. ADX (Average Directional Index) + DI

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Period | 14 | Standard; smooths noise |
| Ranging threshold | ADX < 25 | No directional trend |
| Trending threshold | ADX > 30 | Strong trend, skip mean-reversion |

**Regime rules:**
```
adx < 25  → RANGING:  trade the range (BB mean-reversion)
adx 25-30 → CAUTION:  reduce position size, tighter stops
adx > 30  → TRENDING: skip — do not enter counter-trend
```

**Note:** ADX measures trend strength, NOT direction. A high ADX means price is moving directionally, regardless of direction.

### 4. RSI (Relative Strength Index)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Period | 14 | Standard; matches ATR period for consistency |
| Oversold threshold | < 40 | Confirms lower BB oversold signal |
| Overbought threshold | > 60 | Triggers exit near upper BB |

---

## Per-Pair Configuration

Each pair has its own cost-adjusted minimum width floor. These are derived from:
- **Pool depth** (TVL on Aerodrome)
- **Fee rate** (0.05% for slipstream, 0.15-0.30% for standard)
- **Historical slippage** observed from fills

### Pair Parameters

| Pair | Pool Address | Fee Rate | Min Width | BB Period | ATR Stop | TVL Approx |
|------|-------------|---------|-----------|-----------|----------|------------|
| WETH/USDC | 0xb2cc... | 0.05% | 1.5% | 20 | 1.5× ATR | ~$50M |
| cbBTC/WETH | 0x70ac... | 0.05% | 1.5% | 20 | 1.5× ATR | ~$20M |
| AERO/USDC | 0x6cdc... | 0.30% | 2.0% | 20 | 1.5× ATR | ~$10M |
| ZEN/WETH | 0x0392... | 0.15% | 3.0% | 20 | 1.5× ATR | ~$2M |
| VFY/USDC | Uniswap v4 | 0.30% | 6.0% | 20 | 1.5× ATR | ~$1M |

**Width adjustment rule:** If bandwidth < min_width, skip the pair (not enough range to trade after costs).

---

## Signal Definitions

### Entry Signal (Long / Buy Range)

All conditions must be true simultaneously:

```
1. regime == "RANGING"          # ADX < 25
2. price <= lower_bb            # At or below lower band
3. rsi < 40                     # Confirmed oversold
4. bandwidth >= min_width       # Range wide enough to trade
```

### Exit Signal (Sell / Close Range)

Triggered when ANY of these are true:

```
1. price >= upper_bb            # Touched upper band (primary)
2. rsi > 60                     # Overbought confirmation
3. Trailing stop hit            # price < entry - (1.5 × ATR)
```

### Stop Loss

```
stop_loss = entry_price - (1.5 × atr_at_entry)
```

If price closes below `stop_loss` → position closed at loss.

---

## Signal Flow (per pair, per scan)

```
1. Fetch 1h candles (last 7 days = 168 candles)
2. Calculate SMA(20), STDDEV(20), upper_bb, lower_bb
3. Calculate ATR(14) from 1h
4. Fetch 4h candles (last 7 days = 42 candles)
5. Calculate ADX(14) from 4h candles
6. Determine regime: RANGING / CAUTION / TRENDING
7. If regime == TRENDING → skip pair
8. If regime == CAUTION → halve position size
9. Calculate current RSI(14)
10. Evaluate entry/exit/stop conditions
11. If entry signal → emit signal with range boundaries
```

---

## Range Quality Score

Range quality is a 0–100 score used to filter out marginal setups.

```
score = (bandwidth_pct / min_width) × 50   # width score (0-50)
       + (50 - adx) × 0.5                   # trend score (0-25 max)
       + (60 - rsi) × 0.25                 # oversold bonus (0-5 max)
       + (rsi - 40) × 0.25                 # avoid overbought (0-5 max)

# Clamped to 0-100
```

**Minimum score to enter:** 55 (tunable after backtest)

---

## Data Requirements

| Timeframe | Source | Candles Needed | Use |
|-----------|--------|---------------|-----|
| 1h | CoinGecko `/coins/{id}/ohlc?days=7` | 168 | BB, ATR, RSI |
| 4h | Aggregated from 1h | 42 | ADX regime filter |

**Note:** CoinGecko OHLCV endpoint does not return volume. Volume is not used in current signal definitions but can be added as a filter (skip if volume < threshold).

---

## Output Schema

```python
{
    "pair": "WETH/USDC",
    "timestamp": "2026-04-15T08:00:00Z",
    "regime": "RANGING",           # RANGING | CAUTION | TRENDING
    "bb": {
        "upper": 3245.50,
        "mid":  3200.00,
        "lower": 3154.50,
        "bandwidth_pct": 2.84      # (upper - lower) / mid × 100
    },
    "atr": 18.42,
    "adx": 22.1,
    "rsi": 35.4,
    "quality_score": 68,
    "signal": "ENTER_LONG",        # ENTER_LONG | EXIT_LONG | HOLD | SKIP
    "entry_price": 3154.50,        # lower_bb
    "stop_loss": 3127.37,          # lower_bb - 1.5 × atr
    "target": 3245.50,             # upper_bb
    "min_width_met": True,
    "reason": "lower_bb_touch + rsi_oversold + ranging"
}
```

---

## Implementation Notes

1. **Parallel to v1:** This engine runs alongside `support_resistance.py`, not replacing it. Both log signals. Pipeline uses v1 until v2 is validated.

2. **No volume filter yet:** CoinGecko OHLCV endpoint doesn't return volume. If needed, pull from GeckoTerminal 1m candles (with rate limit awareness).

3. **RSI period:** Standard is 14, matching ATR. Can be tuned shorter (7) for more responsiveness or longer (21) for smoother signals.

4. **BB period:** 20 is standard. For shorter timeframes (e.g., 15m candles), use period=20 still but with more candles.

5. **ADX lag:** ADX is a lagging indicator. In fast markets, price can move significantly before ADX crosses 30. Use price momentum filter as a leading supplement if needed.

---

## Backtest Validation (Phase 5)

Before live trading, validate:
- [ ] Win rate ≥ 55% after costs across 30+ days
- [ ] No pair shows consistently negative expectancy
- [ ] Slippage modeled within 0.5% of simulated
- [ ] Regime filter actually avoids trending losses (compare trades taken in ADX>30 vs ADX<25)
