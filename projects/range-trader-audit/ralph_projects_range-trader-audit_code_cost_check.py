"""
cost_check.py — Pre-buy cost estimation for Range trader.

Supports two paths:
- Slipstream CL (WETH-quoted): uses quoter contract for expected output
- Classic Aerodrome (USDC-quoted): uses getAmountsOut

Estimates slippage vs market price + gas before committing.
"""
import os, json
from pathlib import Path
from datetime import datetime, timezone

_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip().replace('export ', '').strip(), _v.strip())

AERODROME_ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'
AERODROME_FACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da'
WETH_ADDRESS = '0x4200000000000000000000000000000000000006'
USDC_ADDRESS = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'

ROUTER_ABI_MINIMAL = [
    {'name': 'getAmountsOut', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'amountIn', 'type': 'uint256'},
                {'name': 'routes', 'type': 'tuple[]',
                 'components': [{'name': 'from', 'type': 'address'},
                                {'name': 'to', 'type': 'address'},
                                {'name': 'stable', 'type': 'bool'},
                                {'name': 'factory', 'type': 'address'}]}],
     'outputs': [{'name': '', 'type': 'uint256[]'}]}
]

BUY_GAS_ESTIMATE = 300000   # conservative for Slipstream CL


def estimate_cost(token_address: str, symbol: str, amount_in: float,
                  eth_price_usd: float, market_price_usd: float,
                  position_usd: float, is_slipstream: bool = False,
                  token_decimals: int = 18) -> dict:
    """
    Estimate slippage + gas before executing a range trader buy.

    Args:
        token_address: ERC20 token on Base
        symbol: Token symbol (logging)
        amount_in: ETH (Slipstream) or USDC amount to spend
        eth_price_usd: Current ETH/USD price
        market_price_usd: Current token market price in USD
        position_usd: Position size in USD
        is_slipstream: True for WETH-quoted CL pools (e.g. ZEN)
        token_decimals: Token decimal places
    """
    result = {
        'passes': True,
        'slippage_pct': 0.0,
        'gas_eth': 0.0,
        'gas_pct': 0.0,
        'total_cost_pct': 0.0,
        'reason': ''
    }
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(os.environ.get('BASE_RPC_URL', 'https://mainnet.base.org')))

        if not is_slipstream:
            # Classic Aerodrome: USDC → token via getAmountsOut
            router = w3.eth.contract(
                address=Web3.to_checksum_address(AERODROME_ROUTER),
                abi=ROUTER_ABI_MINIMAL
            )
            token_cs = Web3.to_checksum_address(token_address)
            usdc_cs = Web3.to_checksum_address(USDC_ADDRESS)
            factory_cs = Web3.to_checksum_address(AERODROME_FACTORY)
            amount_wei = int(amount_in * 1e6)  # USDC has 6 decimals
            routes = [{'from': usdc_cs, 'to': token_cs, 'stable': False, 'factory': factory_cs}]
            amounts = router.functions.getAmountsOut(amount_wei, routes).call()
            expected_tokens = amounts[-1] / (10 ** token_decimals)
            if market_price_usd > 0:
                ideal_tokens = amount_in / market_price_usd
                if ideal_tokens > 0:
                    slippage = (ideal_tokens - expected_tokens) / ideal_tokens * 100
                    result['slippage_pct'] = max(0.0, slippage)
        else:
            # Slipstream: WETH → token — no simple getAmountsOut
            # Use 1% as conservative estimate for CL pools (typically lower)
            result['slippage_pct'] = 1.0
            result['reason'] = 'slipstream_estimate_conservative'

        # Gas cost
        gas_price_wei = w3.eth.gas_price
        gas_eth = (BUY_GAS_ESTIMATE * gas_price_wei) / 1e18
        gas_usd = gas_eth * eth_price_usd
        result['gas_eth'] = gas_eth
        result['gas_pct'] = (gas_usd / position_usd * 100) if position_usd > 0 else 0.0
        result['total_cost_pct'] = result['slippage_pct'] + result['gas_pct']

    except Exception as e:
        print(f'[COST_CHECK] Estimation error for {symbol}: {e}')
        result['reason'] = f'estimation_error: {e}'
        result['total_cost_pct'] = 0.0

    return result


def check_cost_threshold(token_address: str, symbol: str, amount_in: float,
                         eth_price_usd: float, market_price_usd: float,
                         position_usd: float, max_cost_pct: float,
                         is_slipstream: bool = False, token_decimals: int = 18) -> dict:
    if max_cost_pct <= 0:
        return {'passes': True, 'total_cost_pct': 0.0, 'reason': 'check_disabled'}

    est = estimate_cost(token_address, symbol, amount_in, eth_price_usd,
                        market_price_usd, position_usd, is_slipstream, token_decimals)

    if est['total_cost_pct'] > max_cost_pct:
        est['passes'] = False
        est['reason'] = (f'cost_too_high: {est["total_cost_pct"]:.2f}% > threshold {max_cost_pct:.1f}% '
                         f'(slippage {est["slippage_pct"]:.2f}% + gas {est["gas_pct"]:.2f}%)')
        print(f'[COST_CHECK] {symbol}: SKIP — {est["reason"]}')
    else:
        est['passes'] = True
        print(f'[COST_CHECK] {symbol}: OK — cost {est["total_cost_pct"]:.2f}% ≤ {max_cost_pct:.1f}%')

    est['threshold'] = max_cost_pct
    return est


def log_skipped(data_dir: Path, symbol: str, address: str, score: int,
                position_usd: float, cost_est: dict):
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'reason': 'cost_too_high',
        'symbol': symbol,
        'address': address,
        'score': score,
        'position_usd': position_usd,
        'est_slippage_pct': round(cost_est.get('slippage_pct', 0), 3),
        'est_gas_pct': round(cost_est.get('gas_pct', 0), 3),
        'total_cost_pct': round(cost_est.get('total_cost_pct', 0), 3),
        'threshold': cost_est.get('threshold', 0),
    }
    log_path = data_dir / 'skipped_log.jsonl'
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')
    print(f'[COST_CHECK] {symbol}: logged to skipped_log.jsonl')
