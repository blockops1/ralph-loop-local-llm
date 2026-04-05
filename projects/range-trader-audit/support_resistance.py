"""
support_resistance.py - Swing-point S/R detector tuned for range trading

Tuned for range trading (vs base-trader's momentum version):
  - Longer lookback: 7 candles (vs 3) -- reduces noise, finds cleaner ranges
  - Wider near_support threshold: 8% (vs 5%) -- enter before price fully tests support
  - Tighter near_resistance threshold: 4% (vs 3%) -- sell sooner, protect gains
  - range_quality_score(): scores how well-defined and tradeable the range is (0-100)
  - get_entry_price(): support * 1.005 (0.5% above support)
  - get_exit_price(): resistance * 0.995 (0.5% below resistance)

OHLCV format (list of dicts):
  [{'timestamp': 1234567890, 'open': 1.0, 'high': 1.2, 'low': 0.9, 'close': 1.1, 'volume': 10000.0}, ...]
  Sorted oldest-first. Minimum 20 candles required.
"""

from typing import Optional


def calculate_sr(candles: list, lookback: int = 7) -> Optional[dict]:
    """
    Calculate support and resistance levels from OHLCV candle data.
    Tuned for range trading: lookback=7, near_support=8%, near_resistance=4%.

    Returns dict with:
      support, resistance, current_price,
      pct_above_support, pct_below_resistance,
      swing_lows, swing_highs,
      near_support, near_resistance,
      entry_price, exit_price

    Returns None if fewer than 2 swing highs or 2 swing lows found.
    """
    min_candles = 10
    if len(candles) < min_candles:
        return None

    min_for_swing = lookback * 2 + 1
    if len(candles) < min_for_swing:
        return None

    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]

    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(candles) - lookback):
        # Swing high: candle high > all highs in lookback window on both sides
        left_highs = highs[i - lookback:i]
        right_highs = highs[i + 1:i + lookback + 1]
        if highs[i] > max(left_highs) and highs[i] > max(right_highs):
            swing_highs.append(highs[i])

        # Swing low: candle low < all lows in lookback window on both sides
        left_lows = lows[i - lookback:i]
        right_lows = lows[i + 1:i + lookback + 1]
        if lows[i] < min(left_lows) and lows[i] < min(right_lows):
            swing_lows.append(lows[i])

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    # Use 2 most recent swing highs and lows
    recent_highs = swing_highs[-2:]
    recent_lows = swing_lows[-2:]

    resistance = sum(recent_highs) / len(recent_highs)
    support = sum(recent_lows) / len(recent_lows)
    current_price = candles[-1]['close']

    if support <= 0 or resistance <= 0:
        return None

    pct_above_support = (current_price - support) / support * 100
    pct_below_resistance = (resistance - current_price) / resistance * 100

    entry_price = support * 1.005
    exit_price = resistance * 0.995

    return {
        'support': support,
        'resistance': resistance,
        'current_price': current_price,
        'pct_above_support': pct_above_support,
        'pct_below_resistance': pct_below_resistance,
        'swing_lows': recent_lows,
        'swing_highs': recent_highs,
        'near_support': 0 <= pct_above_support <= 8.0,  # must be ABOVE support, within 8%
        'near_resistance': pct_below_resistance <= 4.0,  # tightened from 5% — sell closer to resistance
        'entry_price': entry_price,
        'exit_price': exit_price,
    }


def _calculate_sr_with_lookback(candles: list, lookback: int) -> Optional[dict]:
    """
    Internal helper: calculate S/R with a specific lookback value.
    Returns None if fewer than 2 swing highs or 2 swing lows found.
    """
    min_candles = 10
    if len(candles) < min_candles:
        return None

    min_for_swing = lookback * 2 + 1
    if len(candles) < min_for_swing:
        return None

    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]

    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(candles) - lookback):
        # Swing high: candle high > all highs in lookback window on both sides
        left_highs = highs[i - lookback:i]
        right_highs = highs[i + 1:i + lookback + 1]
        if highs[i] > max(left_highs) and highs[i] > max(right_highs):
            swing_highs.append(highs[i])

        # Swing low: candle low < all lows in lookback window on both sides
        left_lows = lows[i - lookback:i]
        right_lows = lows[i + 1:i + lookback + 1]
        if lows[i] < min(left_lows) and lows[i] < min(right_lows):
            swing_lows.append(lows[i])

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    # Use 2 most recent swing highs and lows
    recent_highs = swing_highs[-2:]
    recent_lows = swing_lows[-2:]

    resistance = sum(recent_highs) / len(recent_highs)
    support = sum(recent_lows) / len(recent_lows)
    current_price = candles[-1]['close']

    if support <= 0 or resistance <= 0:
        return None

    pct_above_support = (current_price - support) / support * 100
    pct_below_resistance = (resistance - current_price) / resistance * 100

    entry_price = support * 1.005
    exit_price = resistance * 0.995

    return {
        'support': support,
        'resistance': resistance,
        'current_price': current_price,
        'pct_above_support': pct_above_support,
        'pct_below_resistance': pct_below_resistance,
        'swing_lows': recent_lows,
        'swing_highs': recent_highs,
        'near_support': 0 <= pct_above_support <= 8.0,  # must be ABOVE support, within 8%
        'near_resistance': pct_below_resistance <= 4.0,  # tightened from 5% — sell closer to resistance
        'entry_price': entry_price,
        'exit_price': exit_price,
    }


def calculate_sr_with_fallback(candles: list, hourly_candles: Optional[list] = None) -> Optional[dict]:
    """
    Calculate support and resistance with a fallback cascade.
    
    Cascade order:
      1. daily lookback=7 (cleanest ranges)
      2. daily lookback=5
      3. daily lookback=3
      4. hourly lookback=5 (if hourly_candles provided)
    
    Returns dict with S/R data plus:
      - 'sr_method': 'daily_7', 'daily_5', 'daily_3', or 'hourly_5'
      - 'min_quality': minimum range_quality_score required for this method
    
    Quality thresholds by method:
      - daily_7: 50 (current behavior)
      - daily_5: 55
      - daily_3: 65
      - hourly_5: 60
    
    Returns None if no method finds valid S/R.
    """
    # Define cascade: (lookback, method_name, min_quality)
    cascade = [
        (7, 'daily_7', 50),
        (5, 'daily_5', 55),
        (3, 'daily_3', 65),
    ]
    
    # Try daily lookbacks first
    for lookback, method_name, min_quality in cascade:
        result = _calculate_sr_with_lookback(candles, lookback)
        if result is not None:
            result['sr_method'] = method_name
            result['min_quality'] = min_quality
            return result
    
    # If hourly candles provided, try hourly lookback=5
    if hourly_candles is not None:
        result = _calculate_sr_with_lookback(hourly_candles, 5)
        if result is not None:
            result['sr_method'] = 'hourly_5'
            result['min_quality'] = 60
            return result
    
    return None


def range_quality_score(candles: list, sr: dict) -> int:
    """
    Score how well-defined and tradeable a range is (0-100).
    Returns 0 if the range is not worth trading.

    Scoring:
      - Range width >= 10%:        +30 pts
      - Range width >= 20%:        +10 pts bonus (total 40)
      - Support touches >= 3:      +25 pts
      - Support touches >= 5:      +10 pts bonus (total 35)
      - Resistance touches >= 3:   +20 pts
      - Range age >= 14 days:      +10 pts (daily candles assumed)
      - Range age >= 30 days:      +5 pts bonus (total 15)

    Max = 100. Threshold for trading: >= 50.
    """
    if sr is None:
        return 0

    support = sr['support']
    resistance = sr['resistance']
    score = 0

    # Range width
    if support > 0:
        range_pct = (resistance - support) / support * 100
        if range_pct >= 10:
            score += 30
        if range_pct >= 20:
            score += 10

    # Count support touches: candle lows within 3% of support
    support_touches = sum(
        1 for c in candles
        if abs(c['low'] - support) / support <= 0.03
    )
    if support_touches >= 3:
        score += 25
    if support_touches >= 5:
        score += 10

    # Count resistance touches: candle highs within 3% of resistance
    resistance_touches = sum(
        1 for c in candles
        if abs(c['high'] - resistance) / resistance <= 0.03
    )
    if resistance_touches >= 3:
        score += 20

    # Range age: number of candles (daily assumed) since oldest swing point
    range_age = len(candles)
    if range_age >= 14:
        score += 10
    if range_age >= 30:
        score += 5

    return min(score, 100)


def get_entry_price(sr: dict) -> Optional[float]:
    """Buy just above support: support * 1.005"""
    if sr is None:
        return None
    return sr['support'] * 1.005


def get_exit_price(sr: dict) -> Optional[float]:
    """Sell just below resistance: resistance * 0.995"""
    if sr is None:
        return None
    return sr['resistance'] * 0.995


def get_stop_loss(sr: dict) -> Optional[float]:
    """Stop loss 3% below support -- if support breaks, exit fast"""
    if sr is None:
        return None
    return sr['support'] * 0.97
