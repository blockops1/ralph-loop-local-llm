"""
range_engine.py - BB+ATR+ADX signal engine for range trading

Implements technical indicators and signal generation for mean-reversion range trading:
  - Bollinger Bands (BB) for entry/exit levels
  - Average True Range (ATR) for volatility-based stops
  - Average Directional Index (ADX) for regime detection
  - Relative Strength Index (RSI) for entry confirmation

Signal logic:
  - ENTER_LONG: Price near lower BB, RSI < 40, ADX < 25 (ranging market)
  - EXIT_LONG: RSI > 60 (overbought)
  - SKIP: ADX > 30 (trending market) or bandwidth < min_width
  - HOLD: Default when no signal conditions met

OHLCV format (list of dicts):
  [{'timestamp': '2024-01-01T00:00:00', 'open': 1.0, 'high': 1.2, 'low': 0.9, 'close': 1.1, 'volume': 10000.0}, ...]
  Sorted oldest-first.
"""

from datetime import datetime
from typing import Optional

from range_engine_config import (
    PER_PAIR_CONFIG,
    Regime,
    Signal,
    RSI_OVERSOLD,
    RSI_OVERBOUGHT_EXIT,
    REGIME_RANGING_ADX_MAX,
    REGIME_CAUTION_ADX_MIN,
    REGIME_CAUTION_ADX_MAX,
    REGIME_TRENDING_ADX_MIN,
    ATR_STOP_MULT,
    BB_STD_DEV_MULT,
)


# =============================================================================
# Helper Functions
# =============================================================================

def _ema(values: list, period: int) -> list:
    """
    Calculate Exponential Moving Average.
    
    Args:
        values: List of numeric values
        period: EMA period
    
    Returns:
        List of EMA values (same length as input, with None for first period-1 values)
    """
    if not values or period <= 0:
        return []
    
    multiplier = 2.0 / (period + 1)
    ema_values = [None] * len(values)
    
    # First EMA value is SMA of first 'period' values
    if len(values) >= period:
        ema_values[period - 1] = sum(values[:period]) / period
        for i in range(period, len(values)):
            ema_values[i] = (values[i] - ema_values[i - 1]) * multiplier + ema_values[i - 1]
    else:
        # Not enough values for full EMA, use SMA
        ema_values[-1] = sum(values) / len(values)
    
    return ema_values


def _sma(values: list, period: int) -> float:
    """
    Calculate Simple Moving Average of the last 'period' values.
    
    Args:
        values: List of numeric values
        period: SMA period
    
    Returns:
        SMA value
    """
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    return sum(values[-period:]) / period


# =============================================================================
# Bollinger Bands
# =============================================================================

def calculate_bb(candles: list, period: int = 20) -> dict:
    """
    Calculate Bollinger Bands.
    
    Args:
        candles: List of OHLCV dicts (oldest first)
        period: SMA period for middle band
    
    Returns:
        dict with upper, mid, lower, bandwidth, bandwidth_pct
    """
    if len(candles) < period:
        return {
            'upper': 0.0,
            'mid': 0.0,
            'lower': 0.0,
            'bandwidth': 0.0,
            'bandwidth_pct': 0.0,
        }
    
    # Use all available closes for calculation (not just last 'period')
    # This provides a more stable baseline when more data is available
    closes = [c['close'] for c in candles]
    
    # Calculate SMA (middle band) using all available data
    mid = sum(closes) / len(closes)
    
    # Calculate standard deviation (population std dev)
    variance = sum((p - mid) ** 2 for p in closes) / len(closes)
    std_dev = variance ** 0.5
    
    # Calculate bands
    upper = mid + (BB_STD_DEV_MULT * std_dev)
    lower = mid - (BB_STD_DEV_MULT * std_dev)
    
    # Calculate bandwidth
    bandwidth = upper - lower
    bandwidth_pct = (bandwidth / mid) * 100 if mid > 0 else 0.0
    
    return {
        'upper': upper,
        'mid': mid,
        'lower': lower,
        'bandwidth': bandwidth,
        'bandwidth_pct': bandwidth_pct,
    }


# =============================================================================
# True Range
# =============================================================================

def calculate_tr(high: float, low: float, prev_close: float) -> float:
    """
    Calculate True Range for a single candle.
    
    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    
    Args:
        high: Current candle high
        low: Current candle low
        prev_close: Previous candle close
    
    Returns:
        True Range value
    """
    hl = high - low
    hc = abs(high - prev_close)
    lc = abs(low - prev_close)
    return max(hl, hc, lc)


# =============================================================================
# Average True Range
# =============================================================================

def calculate_atr(candles: list, period: int = 14) -> float:
    """
    Calculate Average True Range (ATR).
    
    ATR is the SMA of True Range values over the period.
    
    Args:
        candles: List of OHLCV dicts (oldest first)
        period: ATR period
    
    Returns:
        ATR value
    """
    if len(candles) < period + 1:
        return 0.0
    
    tr_values = []
    
    for i in range(1, len(candles)):
        tr = calculate_tr(
            high=candles[i]['high'],
            low=candles[i]['low'],
            prev_close=candles[i - 1]['close']
        )
        tr_values.append(tr)
    
    recent_tr = tr_values[-period:]
    atr = sum(recent_tr) / len(recent_tr)
    
    return atr


# =============================================================================
# Directional Movement
# =============================================================================

def _calculate_dm(high: float, low: float, prev_high: float, prev_low: float) -> tuple:
    """
    Calculate Directional Movement (+DM and -DM).
    
    +DM = max(high - prev_high, 0) if (high - prev_high) > (prev_low - low) else 0
    -DM = max(prev_low - low, 0) if (prev_low - low) > (high - prev_high) else 0
    
    Returns:
        Tuple of (+DM, -DM)
    """
    up_move = high - prev_high
    down_move = prev_low - low
    
    if up_move > down_move and up_move > 0:
        return up_move, 0.0
    elif down_move > up_move and down_move > 0:
        return 0.0, down_move
    else:
        return 0.0, 0.0


def calculate_dx(pos_dm: float, neg_dm: float, tr: float) -> float:
    """
    Calculate Directional Movement Index (DX).
    
    DX = 100 * |+DI - -DI| / (+DI + -DI)
    where +DI = 100 * (pos_dm / tr) and -DI = 100 * (neg_dm / tr)
    
    Args:
        pos_dm: Positive directional movement
        neg_dm: Negative directional movement
        tr: True Range
    
    Returns:
        DX value
    """
    if tr <= 0:
        return 0.0
    
    plus_di = 100 * (pos_dm / tr)
    minus_di = 100 * (neg_dm / tr)
    
    di_sum = plus_di + minus_di
    if di_sum > 0:
        dx = 100 * abs(plus_di - minus_di) / di_sum
    else:
        dx = 0.0
    
    return dx


# =============================================================================
# Average Directional Index
# =============================================================================

def calculate_adx(candles: list, period: int = 14) -> float:
    """
    Calculate Average Directional Index (ADX).
    
    ADX is the EMA of DX values over the period.
    
    Args:
        candles: List of OHLCV dicts (oldest first)
        period: ADX period
    
    Returns:
        ADX value
    """
    if len(candles) < period + 1:
        return 0.0
    
    # Calculate TR, +DM, -DM for each candle
    tr_values = []
    plus_dm_values = []
    minus_dm_values = []
    
    for i in range(1, len(candles)):
        # True Range
        tr = calculate_tr(
            high=candles[i]['high'],
            low=candles[i]['low'],
            prev_close=candles[i - 1]['close']
        )
        tr_values.append(tr)
        
        # Directional Movement
        plus_dm, minus_dm = _calculate_dm(
            high=candles[i]['high'],
            low=candles[i]['low'],
            prev_high=candles[i - 1]['high'],
            prev_low=candles[i - 1]['low']
        )
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)
    
    # Smooth TR, +DM, -DM using EMA
    smoothed_tr = _ema(tr_values, period)
    smoothed_plus_dm = _ema(plus_dm_values, period)
    smoothed_minus_dm = _ema(minus_dm_values, period)
    
    # Calculate DX values
    dx_values = []
    for i in range(len(smoothed_tr)):
        if smoothed_tr[i] is not None and smoothed_plus_dm[i] is not None and smoothed_minus_dm[i] is not None:
            dx = calculate_dx(smoothed_plus_dm[i], smoothed_minus_dm[i], smoothed_tr[i])
            dx_values.append(dx)
    
    if not dx_values:
        return 0.0
    
    # ADX is the EMA of DX values
    adx_ema = _ema(dx_values, period)
    
    # Return the last EMA value
    return adx_ema[-1] if adx_ema[-1] is not None else 0.0


# =============================================================================
# Relative Strength Index
# =============================================================================

def calculate_rsi(candles: list, period: int = 14) -> float:
    """
    Calculate Relative Strength Index (RSI).
    
    RSI = 100 - (100 / (1 + RS))
    RS = Average Gain / Average Loss over the period
    
    Args:
        candles: List of OHLCV dicts (oldest first)
        period: RSI period
    
    Returns:
        RSI value (0-100)
    """
    if len(candles) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    
    for i in range(1, len(candles)):
        change = candles[i]['close'] - candles[i - 1]['close']
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    
    recent_gains = gains[-period:]
    recent_losses = losses[-period:]
    
    avg_gain = sum(recent_gains) / len(recent_gains)
    avg_loss = sum(recent_losses) / len(recent_losses)
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


# =============================================================================
# Regime Detection
# =============================================================================

def get_regime(adx: float) -> str:
    """
    Determine market regime based on ADX value.
    
    Args:
        adx: ADX value
    
    Returns:
        Regime string (RANGING, CAUTION, or TRENDING)
    """
    if adx < REGIME_RANGING_ADX_MAX:
        return Regime.RANGING
    elif adx <= REGIME_CAUTION_ADX_MAX:
        return Regime.CAUTION
    else:
        return Regime.TRENDING


# =============================================================================
# Quality Score
# =============================================================================

def calculate_quality_score(bb: dict, adx: float, rsi: float, regime: str) -> float:
    """
    Calculate a quality score for the trading setup (0-100).
    
    Scoring factors:
      - Bandwidth: Wider bands = more room for profit (up to 30 pts)
      - ADX: Lower ADX in ranging = better (up to 30 pts)
      - RSI: Extreme RSI = stronger signal (up to 20 pts)
      - Regime: Ranging regime = bonus (up to 20 pts)
    
    Args:
        bb: Bollinger Bands dict
        adx: ADX value
        rsi: RSI value
        regime: Market regime string
    
    Returns:
        Quality score (0-100)
    """
    score = 0.0
    
    bandwidth_pct = bb.get('bandwidth_pct', 0)
    if bandwidth_pct >= 5:
        score += 30
    elif bandwidth_pct >= 2:
        score += 20
    elif bandwidth_pct >= 1:
        score += 10
    
    if adx < 20:
        score += 30
    elif adx < 25:
        score += 20
    elif adx < 30:
        score += 10
    
    if rsi < 30 or rsi > 70:
        score += 20
    elif rsi < 40 or rsi > 60:
        score += 10
    
    if regime == Regime.RANGING:
        score += 20
    elif regime == Regime.CAUTION:
        score += 10
    
    return min(score, 100.0)


# =============================================================================
# Main Signal Engine
# =============================================================================

def get_range(pair: str, candles_1h: list, candles_4h: list) -> dict:
    """
    Generate range trading signal for a pair.
    
    Args:
        pair: Trading pair string (e.g., 'WETH/USDC')
        candles_1h: 1-hour OHLCV candles (oldest first)
        candles_4h: 4-hour OHLCV candles (oldest first)
    
    Returns:
        dict with signal, entry_price, stop_loss, target, and analysis data
    """
    config = PER_PAIR_CONFIG.get(pair)
    if config is None:
        return {
            'pair': pair,
            'timestamp': datetime.now().isoformat(),
            'regime': Regime.TRENDING,
            'bb': {'upper': 0.0, 'mid': 0.0, 'lower': 0.0, 'bandwidth_pct': 0.0},
            'atr': 0.0,
            'adx': 0.0,
            'rsi': 50.0,
            'quality_score': 0.0,
            'signal': Signal.SKIP,
            'entry_price': 0.0,
            'stop_loss': 0.0,
            'target': 0.0,
            'min_width_met': False,
            'reason': f"Unknown pair: {pair}",
        }
    
    # Calculate indicators on 1h candles
    bb = calculate_bb(candles_1h, period=config.bb_period)
    atr = calculate_atr(candles_1h, period=config.atr_period)
    rsi = calculate_rsi(candles_1h, period=config.rsi_period)
    
    # Calculate ADX on 4h candles (per PRD requirement)
    adx = calculate_adx(candles_4h, period=config.adx_period)
    
    current_price = candles_1h[-1]['close'] if candles_1h else 0.0
    
    regime = get_regime(adx)
    quality_score = calculate_quality_score(bb, adx, rsi, regime)
    
    min_width_met = bb['bandwidth_pct'] >= config.min_width * 100
    
    signal = Signal.HOLD
    entry_price = current_price
    stop_loss = current_price
    target = current_price
    reason = "No clear signal"
    
    if regime == Regime.TRENDING:
        signal = Signal.SKIP
        reason = f"Trending market (ADX={adx:.1f} > {REGIME_TRENDING_ADX_MIN})"
    elif not min_width_met:
        signal = Signal.SKIP
        reason = f"Bandwidth {bb['bandwidth_pct']:.2f}% below minimum {config.min_width * 100:.2f}%"
    else:
        pct_from_lower = (current_price - bb['lower']) / bb['lower'] * 100 if bb['lower'] > 0 else 0
        
        if rsi < RSI_OVERSOLD and pct_from_lower < 5:
            if regime == Regime.RANGING:
                signal = Signal.ENTER_LONG
                entry_price = current_price
                stop_loss = entry_price - (config.atr_stop_mult * atr)
                target = bb['mid']
                reason = f"Price near lower BB, RSI={rsi:.1f} < {RSI_OVERSOLD}, ADX={adx:.1f}"
            elif regime == Regime.CAUTION:
                signal = Signal.ENTER_LONG
                entry_price = current_price
                stop_loss = entry_price - (config.atr_stop_mult * atr)
                target = bb['mid']
                reason = f"Caution zone entry: RSI={rsi:.1f}, ADX={adx:.1f} (reduce position size)"
        elif rsi > RSI_OVERBOUGHT_EXIT:
            signal = Signal.EXIT_LONG
            reason = f"RSI={rsi:.1f} > {RSI_OVERBOUGHT_EXIT} (overbought)"
        else:
            signal = Signal.HOLD
            reason = f"Holding: RSI={rsi:.1f}, ADX={adx:.1f}, bandwidth={bb['bandwidth_pct']:.2f}%"
    
    timestamp = candles_1h[-1].get('timestamp', datetime.now().isoformat()) if candles_1h else datetime.now().isoformat()
    
    return {
        'pair': pair,
        'timestamp': timestamp,
        'regime': regime,
        'bb': {
            'upper': bb['upper'],
            'mid': bb['mid'],
            'lower': bb['lower'],
            'bandwidth_pct': bb['bandwidth_pct'],
        },
        'atr': atr,
        'adx': adx,
        'rsi': rsi,
        'quality_score': quality_score,
        'signal': signal,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'target': target,
        'min_width_met': min_width_met,
        'reason': reason,
    }
