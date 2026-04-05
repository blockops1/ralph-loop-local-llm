"""Entry confirmation engine using 5-minute candle technical indicators."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests
import yaml
from datetime import datetime, timedelta


def load_config():
    """Load configuration from config/config.yaml."""
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_candles(symbol: str, interval: str = "5m", lookback: int = 50) -> list[dict]:
    """Fetch 5m candles from HyperLiquid candle snapshot API.
    
    Args:
        symbol: Coin symbol (e.g., "BTC", "ETH")
        interval: Candle interval (default: "5m")
        lookback: Number of candles to fetch (default: 50)
    
    Returns:
        List of candle dicts with {open, high, low, close, volume, time_ms}
    """
    config = load_config()
    base_url = (
        config["api"]["testnet_url"]
        if config.get("paper_trade", False)
        else config["api"]["mainnet_url"]
    )
    
    end_ms = int(datetime.now().timestamp() * 1000)
    start_ms = end_ms - (lookback * 5 * 60 * 1000)  # lookback * 5 minutes in ms
    
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms
        }
    }
    
    response = requests.post(f"{base_url}/info", json=payload)
    response.raise_for_status()
    
    candles = []
    for candle in response.json():
        candles.append({
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
            "time_ms": int(candle[0])
        })
    
    return candles


def calc_rsi(closes: list[float], period: int = 14) -> float:
    """Calculate RSI using average gains/losses.
    
    Args:
        closes: List of closing prices
        period: RSI period (default: 14)
    
    Returns:
        RSI value between 0 and 100
    """
    if len(closes) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calc_ema(values: list[float], period: int = 21) -> float:
    """Calculate exponential moving average of the last N values.
    
    Args:
        values: List of values
        period: EMA period (default: 21)
    
    Returns:
        Most recent EMA value
    """
    if len(values) < period:
        values = values[:len(values)] if len(values) > 0 else [0.0]
    
    multiplier = 2 / (period + 1)
    
    if len(values) < period:
        # Use simple average as initial value if not enough data
        ema = sum(values) / len(values)
    else:
        # Start with SMA of first period values
        ema = sum(values[:period]) / period
        
        # Apply EMA formula for remaining values
        for value in values[period:]:
            ema = value * multiplier + ema * (1 - multiplier)
    
    return ema


def calc_volume_ratio(volumes: list[float], period: int = 20) -> float:
    """Calculate current volume ratio vs average of last N periods.
    
    Args:
        volumes: List of volume values
        period: Period for average (default: 20)
    
    Returns:
        Ratio of current volume to average (1.0 = average, 1.5 = 50% above average)
    """
    if len(volumes) < period:
        return 1.0
    
    current_volume = volumes[-1]
    avg_volume = sum(volumes[-period:]) / period
    
    if avg_volume == 0:
        return 1.0
    
    return current_volume / avg_volume


def check_entry_conditions(symbol: str, direction: str, config: dict) -> dict:
    """Check if entry conditions are met for a given symbol and direction.
    
    Args:
        symbol: Coin symbol (e.g., "BTC", "ETH")
        direction: "LONG" or "SHORT"
        config: Configuration dict (unused but kept for API consistency)
    
    Returns:
        Dict with {confirmed, rsi, ema21, current_price, volume_ratio, reason}
    """
    candles = get_candles(symbol, interval="5m", lookback=50)
    
    if len(candles) < 21:
        return {
            "confirmed": False,
            "rsi": None,
            "ema21": None,
            "current_price": None,
            "volume_ratio": None,
            "reason": "Insufficient candle data"
        }
    
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    
    current_price = closes[-1]
    rsi = calc_rsi(closes, period=14)
    ema21 = calc_ema(closes, period=21)
    volume_ratio = calc_volume_ratio(volumes, period=20)
    
    reason_parts = []
    all_conditions_met = True
    
    if direction == "LONG":
        # LONG conditions: RSI < 65, price <= EMA21, volume ratio > 1.5
        if rsi >= 65:
            all_conditions_met = False
            reason_parts.append(f"RSI {rsi:.2f} >= 65")
        if current_price < ema21:
            all_conditions_met = False
            reason_parts.append(f"Price {current_price:.2f} < EMA21 {ema21:.2f} (below uptrend)")
        if volume_ratio <= 1.5:
            all_conditions_met = False
            reason_parts.append(f"Volume ratio {volume_ratio:.2f} <= 1.5")
    
    elif direction == "SHORT":
        # SHORT conditions: RSI > 35, price >= EMA21, volume ratio > 1.5
        if rsi <= 35:
            all_conditions_met = False
            reason_parts.append(f"RSI {rsi:.2f} <= 35")
        if current_price < ema21:
            all_conditions_met = False
            reason_parts.append(f"Price {current_price:.2f} < EMA21 {ema21:.2f}")
        if volume_ratio <= 1.5:
            all_conditions_met = False
            reason_parts.append(f"Volume ratio {volume_ratio:.2f} <= 1.5")
    
    else:
        return {
            "confirmed": False,
            "rsi": None,
            "ema21": None,
            "current_price": None,
            "volume_ratio": None,
            "reason": f"Invalid direction: {direction}"
        }
    
    reason = "; ".join(reason_parts) if reason_parts else "All conditions met"
    
    return {
        "confirmed": all_conditions_met,
        "rsi": rsi,
        "ema21": ema21,
        "current_price": current_price,
        "volume_ratio": volume_ratio,
        "reason": reason
    }
