"""HyperLiquid SDK wrapper for trading operations."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml
import os
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants


def load_config():
    """Load configuration from config/config.yaml."""
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_env_vars():
    """Load environment variables from ~/.hermes/.env file."""
    env_path = Path.home() / ".hermes" / ".env"
    env_vars = {}
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env_vars[key.strip()] = value.strip()
    return env_vars


def get_info_client():
    """Return a read-only Info client (no wallet needed)."""
    # Always use mainnet for market data; paper_trade only affects order routing
    return Info(skip_ws=True)


def get_client():
    """Initialize and return (info, exchange, config). Requires wallet env vars."""
    config = load_config()
    env_vars = load_env_vars()

    account_address = env_vars.get("HYPERLIQUID_WALLET_ADDRESS")
    secret_key = env_vars.get("HYPERLIQUID_PRIVATE_KEY")

    if not account_address or not secret_key:
        raise ValueError("Missing wallet address or private key in environment variables")

    info = Info(skip_ws=True)
    exchange = Exchange(info, account_address, secret_key)

    return info, exchange, config


def get_meta():
    """Returns perpetuals metadata (universe + margin tables)."""
    try:
        info = get_info_client()
        meta, _ = info.meta_and_asset_ctxs()
        return meta
    except Exception as e:
        raise RuntimeError(f"Failed to get metadata: {e}")


def get_asset_contexts():
    """Returns mark price, funding, OI for all assets.
    Returns list of dicts with keys: funding, openInterest, prevDayPx,
    dayNtlVlm, premium, oraclePx, markPx, midPx, impactPxs, dayBaseVlm
    Indexed in same order as meta['universe'].
    """
    try:
        info = get_info_client()
        _, ctxs = info.meta_and_asset_ctxs()
        return ctxs
    except Exception as e:
        raise RuntimeError(f"Failed to get asset contexts: {e}")


def get_meta_and_asset_ctxs():
    """Returns (meta, asset_contexts) together (one API call)."""
    try:
        info = get_info_client()
        return info.meta_and_asset_ctxs()
    except Exception as e:
        raise RuntimeError(f"Failed to get meta+asset contexts: {e}")


def get_user_state():
    """Returns account positions + margin summary."""
    try:
        info, exchange, config = get_client()
        account_address = exchange.wallet.address
        return info.user_state(account_address)
    except Exception as e:
        raise RuntimeError(f"Failed to get user state: {e}")


def get_open_orders():
    """Returns open orders."""
    try:
        info, exchange, config = get_client()
        account_address = exchange.wallet.address
        return info.open_orders(account_address)
    except Exception as e:
        raise RuntimeError(f"Failed to get open orders: {e}")


def place_order(asset_index: int, is_buy: bool, size: float, price: float, order_type: dict, reduce_only: bool = False):
    """Places an order or logs it in paper trade mode."""
    try:
        config = load_config()
        if config.get("paper_trade", False):
            print(f"[PAPER TRADE] Would place order: asset_index={asset_index}, is_buy={is_buy}, size={size}, price={price}, order_type={order_type}, reduce_only={reduce_only}")
            return {"status": "ok", "mock": True}

        _, exchange, _ = get_client()
        return exchange.order(asset_index, is_buy, size, price, order_type, reduce_only=reduce_only)
    except Exception as e:
        raise RuntimeError(f"Failed to place order: {e}")


def cancel_order(asset_index: int, order_id: int):
    """Cancels an order or logs it in paper trade mode."""
    try:
        config = load_config()
        if config.get("paper_trade", False):
            print(f"[PAPER TRADE] Would cancel order: asset_index={asset_index}, order_id={order_id}")
            return {"status": "ok", "mock": True}

        _, exchange, _ = get_client()
        return exchange.cancel(asset_index, order_id)
    except Exception as e:
        raise RuntimeError(f"Failed to cancel order: {e}")


def get_user_fills(start_time_ms: int):
    """Returns recent fills."""
    try:
        info, exchange, _ = get_client()
        account_address = exchange.wallet.address
        return info.user_fills_by_time(account_address, start_time_ms)
    except Exception as e:
        raise RuntimeError(f"Failed to get user fills: {e}")


def get_funding_history(coin: str, start_time_ms: int):
    """Returns funding history for a coin."""
    try:
        info = get_info_client()
        return info.funding_history(coin, start_time_ms)
    except Exception as e:
        raise RuntimeError(f"Failed to get funding history: {e}")
