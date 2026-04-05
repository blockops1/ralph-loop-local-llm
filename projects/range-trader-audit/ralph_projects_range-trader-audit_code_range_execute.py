#!/usr/bin/env python3
"""
range_execute.py - Live Aerodrome swap execution for range trader

Executes USDC<->token swaps via Aerodrome Router on Base mainnet.
Called by range_pipeline.py when a BUY/SELL signal passes threshold.
"""

import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Load .env
_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip().replace('export ', '').strip(), _v.strip())

from web3 import Web3
from eth_account import Account
import requests

# Constants
BASE_RPC = 'https://mainnet.base.org'
AERODROME_ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'
AERODROME_FACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da'
# Aerodrome Slipstream (CL) SwapRouter -- for concentrated liquidity pools
SLIPSTREAM_ROUTER = '0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5'
USDC_ADDRESS = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
WETH_ADDRESS = '0x4200000000000000000000000000000000000006'
USDC_DECIMALS = 6
WETH_DECIMALS = 18
SLIPPAGE_DEFAULT = 0.03    # 3.0% (bumped from 1.5% - volatile market causing reverts 2026-03-22)
SLIPPAGE_MAX = 0.05        # 5% absolute cap
DEADLINE_SECONDS = 120

# Per-token routing config for Aerodrome Slipstream (CL) pools.
# Format: token_address_lower -> {'quote': quote_token_address, 'tick_spacing': int}
# Tick spacings verified on-chain 2026-03-22.
# CRV (Curve-base) and ETHFI (Uniswap V4-base) are intentionally excluded - watch-only.
SLIPSTREAM_TOKENS = {
    # WETH-quoted pairs
    '0x4200000000000000000000000000000000000006': {  # WETH - WETH/USDC 0.05%
        'quote': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        'tick_spacing': 10,
    },
    '0x63706e401c06ac8513145b7687a14804d17f814b': {  # AAVE / WETH 0.3%
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 200,
    },
    '0xb0ffa8000886e57f86dd5264b9582b2ad87b2b91': {  # W / WETH 0.3%
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 200,
    },
    '0xf43eb8de897fbc7f2502483b2bef7bb9ea179229': {  # ZEN / WETH 0.15%
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 100,
    },
    '0x82321f3beb69f503380d6b233857d5c43562e2d0': {  # AERO / WETH 1% (SLIPSTREAM)
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 400,
    },
    '0x7ec6c9d993d9832aa654593f2dbc21303650bc6c': {  # VVV / WETH 0.05%
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 10,
    },
    '0xa4463789e8f3c6a599b3dfb608dde55513bcf289': {  # ZRO / WETH 0.05%
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 10,
    },
    '0x4e829f8a5213c42535ab84aa40bd4adcce9cba02': {  # BRETT / WETH 1%
        'quote': '0x4200000000000000000000000000000000000006',
        'tick_spacing': 400,
    },
    # USDC-quoted pairs
    '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf': {  # cbBTC / USDC 0.05%
        'quote': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        'tick_spacing': 10,
    },
    '0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d': {  # AERO / USDC (Classic)
        'quote': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        'tick_spacing': 0,
    },
    '0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59': {  # WETH / USDC 0.05% (SLIPSTREAM)
        'quote': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        'tick_spacing': 10,
    },
}

# Tokens with no supported execution path - signal/scan only, never execute.
WATCH_ONLY_TOKENS = {
    '0x8ee73c484a26e0a5df2ee2a4960b789967dd0415': {  # CRV - Curve pool, no Aerodrome router
        'reason': 'Curve pool on Base, no Aerodrome router'
    },
    '0x6c240dda6b5c336df09a4d011139beaaa1ea2aa2': {  # ETHFI - Uniswap V4 pool
        'reason': 'Uniswap V4 pool, no V4 router'
    },
    '0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196': {  # LINK - thin pool ($161K TVL)
        'reason': 'Pool too thin for range trading'
    },
    '0x9e1028f5f1d5ede59748ffcee5532509976840e0': {  # COMP - pool too thin ($21K TVL)
        'reason': 'Pool too thin for range trading'
    },
    '0xf0836a95054afe930cf94bd7cb029ce2ee96efc7': {  # ARB - no Aerodrome pool
        'reason': 'No Aerodrome pool found for ARB'
    },
    '0x4200000000000000000000000000000000000006': {  # WETH - base asset
        'reason': 'Base asset, use USDC pair instead'
    },
}

SLIPSTREAM_ROUTER_ABI = [
    {
        'name': 'exactInputSingle',
        'type': 'function',
        'stateMutability': 'payable',
        'inputs': [{'name': 'params', 'type': 'tuple', 'components': [
            {'name': 'tokenIn', 'type': 'address'},
            {'name': 'tokenOut', 'type': 'address'},
            {'name': 'tickSpacing', 'type': 'int24'},
            {'name': 'recipient', 'type': 'address'},
            {'name': 'deadline', 'type': 'uint256'},
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'amountOutMinimum', 'type': 'uint256'},
            {'name': 'sqrtPriceLimitX96', 'type': 'uint160'},
        ]}],
        'outputs': [{'name': 'amountOut', 'type': 'uint256'}]
    }
]

ROUTER_ABI = [
    {
        'name': 'swapExactTokensForTokensSupportingFeeOnTransferTokens',
        'type': 'function',
        'stateMutability': 'nonpayable',
        'inputs': [
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'amountOutMin', 'type': 'uint256'},
            {'name': 'routes', 'type': 'tuple[]', 'components': [
                {'name': 'from', 'type': 'address'},
                {'name': 'to', 'type': 'address'},
                {'name': 'stable', 'type': 'bool'},
                {'name': 'factory', 'type': 'address'}
            ]},
            {'name': 'to', 'type': 'address'},
            {'name': 'deadline', 'type': 'uint256'}
        ],
        'outputs': []
    },
    {
        'name': 'getAmountsOut',
        'type': 'function',
        'stateMutability': 'view',
        'inputs': [
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'routes', 'type': 'tuple[]', 'components': [
                {'name': 'from', 'type': 'address'},
                {'name': 'to', 'type': 'address'},
                {'name': 'stable', 'type': 'bool'},
                {'name': 'factory', 'type': 'address'}
            ]}
        ],
        'outputs': [{'name': 'amounts', 'type': 'uint256[]'}]
    },
    {
        'name': 'swapExactETHForTokensSupportingFeeOnTransferTokens',
        'type': 'function',
        'stateMutability': 'payable',
        'inputs': [
            {'name': 'amountOutMin', 'type': 'uint256'},
            {'name': 'routes', 'type': 'tuple[]', 'components': [
                {'name': 'from', 'type': 'address'},
                {'name': 'to', 'type': 'address'},
                {'name': 'stable', 'type': 'bool'},
                {'name': 'factory', 'type': 'address'}
            ]},
            {'name': 'to', 'type': 'address'},
            {'name': 'deadline', 'type': 'uint256'}
        ],
        'outputs': []
    },
    {
        'name': 'swapExactTokensForETHSupportingFeeOnTransferTokens',
        'type': 'function',
        'stateMutability': 'nonpayable',
        'inputs': [
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'amountOutMin', 'type': 'uint256'},
            {'name': 'routes', 'type': 'tuple[]', 'components': [
                {'name': 'from', 'type': 'address'},
                {'name': 'to', 'type': 'address'},
                {'name': 'stable', 'type': 'bool'},
                {'name': 'factory', 'type': 'address'}
            ]},
            {'name': 'to', 'type': 'address'},
            {'name': 'deadline', 'type': 'uint256'}
        ],
        'outputs': []
    }
]

ERC20_ABI = [
    {'name': 'approve', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [{'name': 'spender', 'type': 'address'}, {'name': 'amount', 'type': 'uint256'}],
     'outputs': [{'name': '', 'type': 'bool'}]},
    {'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'decimals', 'type': 'function', 'stateMutability': 'view',
     'inputs': [], 'outputs': [{'name': '', 'type': 'uint8'}]}
]


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(BASE_RPC, request_kwargs={'timeout': 30}))
    if not w3.is_connected():
        raise RuntimeError('Cannot connect to Base RPC')
    return w3


def get_account():
    private_key = os.environ.get('METAMASK_WALLET_PRIVATE_KEY', '')
    if not private_key:
        raise RuntimeError('METAMASK_WALLET_PRIVATE_KEY not set in env')
    if not private_key.startswith('0x'):
        private_key = '0x' + private_key
    return Account.from_key(private_key)


def get_usdc_balance(w3: Web3, address: str) -> float:
    """Get USDC balance in USD float (divides by 10^6)."""
    usdc_cs = Web3.to_checksum_address(USDC_ADDRESS)
    usdc_contract = w3.eth.contract(address=usdc_cs, abi=ERC20_ABI)
    balance_wei = usdc_contract.functions.balanceOf(address).call()
    return float(balance_wei) / (10 ** USDC_DECIMALS)


def _get_nonce(w3, address: str) -> int:
    """
    Get the next safe nonce. Uses 'latest' (confirmed txs only) to avoid
    re-using a nonce from a stuck pending transaction.
    """
    return w3.eth.get_transaction_count(address, 'latest')


def _check_allowance(w3, token_cs, owner: str, spender_cs, amount_wei: int) -> bool:
    """Return True if current allowance >= amount_wei (skip re-approve)."""
    try:
        abi = [{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
                "name":"allowance","outputs":[{"name":"","type":"uint256"}],
                "stateMutability":"view","type":"function"}]
        tok = w3.eth.contract(address=token_cs, abi=abi)
        return tok.functions.allowance(owner, Web3.to_checksum_address(spender_cs)).call() >= amount_wei
    except Exception:
        return False


def _approve(w3, account, token_cs, spender_cs, amount_wei):
    """Approve spender to spend amount_wei of token. Skips if allowance already sufficient."""
    if _check_allowance(w3, token_cs, account.address, spender_cs, amount_wei):
        print(f'[EXECUTE] Allowance already sufficient, skipping approve')
        return None
    tok = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
    gas_price = w3.eth.gas_price
    tx = tok.functions.approve(spender_cs, amount_wei).build_transaction({
        'from': account.address,
        'nonce': _get_nonce(w3, account.address),
        'gas': 100000,
        'maxFeePerGas': gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
        'chainId': 8453,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(h, timeout=90)
    print(f'[EXECUTE] Approval confirmed: {h.hex()}')
    return h.hex()


def _get_token_price(token_address: str) -> float:
    """
    Get token price in USD from GeckoTerminal.
    Falls back to 0.0 on error.
    """
    addr_lower = token_address.lower()
    try:
        _gt_throttle()
        r = requests.get(
            f'https://api.geckoterminal.com/api/v2/networks/base/tokens/{addr_lower}',
            headers={'Accept': 'application/json;version=20230302'},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        price = float(data.get('data', {}).get('attributes', {}).get('price_usd', 0) or 0)
        return price
    except Exception:
        return 0.0


def _get_weth_price() -> float:
    """
    Get WETH price in USD from GeckoTerminal.
    Falls back to 0.0 on error.
    """
    weth_addr = WETH_ADDRESS.lower()
    try:
        _gt_throttle()
        r = requests.get(
            f'https://api.geckoterminal.com/api/v2/networks/base/tokens/{weth_addr}',
            headers={'Accept': 'application/json;version=20230302'},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        price = float(data.get('data', {}).get('attributes', {}).get('price_usd', 0) or 0)
        return price
    except Exception:
        return 0.0


def _gt_throttle():
    """GeckoTerminal rate limit: 10 calls/min free. 12s gap = 5/min (conservative)."""
    import time
    global _gt_last_call
    elapsed = time.time() - _gt_last_call
    if elapsed < 12.0:
        time.sleep(12.0 - elapsed)
    _gt_last_call = time.time()


_gt_last_call: float = 0.0


def _slipstream_buy(w3, account, token_cs, amount_in_wei, amount_in_decimals,
                    cfg, slippage, deadline):
    """Buy token via Aerodrome Slipstream CL pool (WETH -> token)."""
    quote_cs = Web3.to_checksum_address(cfg['quote'])
    tick_spacing = cfg['tick_spacing']
    router = w3.eth.contract(
        address=Web3.to_checksum_address(SLIPSTREAM_ROUTER),
        abi=SLIPSTREAM_ROUTER_ABI
    )
    # amount_in_wei is WETH wei
    weth_contract = w3.eth.contract(address=quote_cs, abi=ERC20_ABI)
    weth_balance = weth_contract.functions.balanceOf(account.address).call()
    if weth_balance == 0:
        raise RuntimeError('No WETH balance to buy with')
    # Use amount_in_wei as the WETH amount (caller must pass WETH wei, not USDC wei)
    _approve(w3, account, quote_cs,
             Web3.to_checksum_address(SLIPSTREAM_ROUTER), amount_in_wei)
    
    # Calculate amountOutMinimum using price-based slippage protection
    amountOutMinimum = 0
    try:
        # Get token price and WETH price
        token_price = _get_token_price(token_cs)
        weth_price = _get_weth_price()
        
        if token_price > 0 and weth_price > 0:
            # expected_tokens = amount_in_weth * eth_price_usd / token_price_usd
            amount_in_weth = amount_in_wei / (10 ** WETH_DECIMALS)
            expected_tokens = amount_in_weth * weth_price / token_price
            # Get token decimals from on-chain call
            token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
            token_decimals = token_contract.functions.decimals().call()
            # amountOutMinimum = int(expected_tokens * (10 ** token_decimals) * (1 - slippage))
            amountOutMinimum = int(expected_tokens * (10 ** token_decimals) * (1 - slippage))
            print(f'[EXECUTE] BUY {token_cs}: expected {expected_tokens:.6f} tokens, min {amountOutMinimum}')
        else:
            print(f'[EXECUTE] WARNING: price lookup failed, using amountOutMinimum=0')
    except Exception as e:
        print(f'[EXECUTE] WARNING: price lookup error {e}, using amountOutMinimum=0')
    
    gas_price = w3.eth.gas_price
    tx = router.functions.exactInputSingle({
        'tokenIn': quote_cs,
        'tokenOut': token_cs,
        'tickSpacing': tick_spacing,
        'recipient': account.address,
        'deadline': deadline,
        'amountIn': amount_in_wei,
        'amountOutMinimum': amountOutMinimum,
        'sqrtPriceLimitX96': 0,
    }).build_transaction({
        'from': account.address,
        'nonce': _get_nonce(w3, account.address),
        'gas': 500000,
        'maxFeePerGas': gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei('0.01', 'gwei'),
        'chainId': 8453,
        'value': 0,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def _slipstream_sell(w3, account, token_cs, amount_in, cfg, slippage, deadline):
    """Sell token via Aerodrome Slipstream CL pool (token -> WETH)."""
    quote_cs = Web3.to_checksum_address(cfg['quote'])
    tick_spacing = cfg['tick_spacing']
    router = w3.eth.contract(
        address=Web3.to_checksum_address(SLIPSTREAM_ROUTER),
        abi=SLIPSTREAM_ROUTER_ABI
    )
    _approve(w3, account, token_cs,
             Web3.to_checksum_address(SLIPSTREAM_ROUTER), amount_in)
    
    # Calculate amountOutMinimum using price-based slippage protection
    amountOutMinimum = 0
    try:
        # Get token price and WETH price
        token_price = _get_token_price(token_cs)
        weth_price = _get_weth_price()
        
        if token_price > 0 and weth_price > 0:
            # Get token decimals from on-chain call
            token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
            token_decimals = token_contract.functions.decimals().call()
            # expected_weth = (amount_in_tokens / (10 ** token_decimals)) * token_price_usd / eth_price_usd
            amount_in_tokens = amount_in / (10 ** token_decimals)
            expected_weth = amount_in_tokens * token_price / weth_price
            # amountOutMinimum = int(expected_weth * (10 ** 18) * (1 - slippage))
            amountOutMinimum = int(expected_weth * (10 ** WETH_DECIMALS) * (1 - slippage))
            print(f'[EXECUTE] SELL {token_cs}: expected {expected_weth:.6f} WETH, min {amountOutMinimum}')
        else:
            print(f'[EXECUTE] WARNING: price lookup failed, using amountOutMinimum=0')
    except Exception as e:
        print(f'[EXECUTE] WARNING: price lookup error {e}, using amountOutMinimum=0')
    
    gas_price = w3.eth.gas_price
    tx = router.functions.exactInputSingle({
        'tokenIn': token_cs,
        'tokenOut': quote_cs,
        'tickSpacing': tick_spacing,
        'recipient': account.address,
        'deadline': deadline,
        'amountIn': amount_in,
        'amountOutMinimum': amountOutMinimum,
        'sqrtPriceLimitX96': 0,
    }).build_transaction({
        'from': account.address,
        'nonce': _get_nonce(w3, account.address),
        'gas': 500000,
        'maxFeePerGas': gas_price * 2,
        'maxPriorityFeePerGas': w3.to_wei('0.01', 'gwei'),
        'chainId': 8453,
        'value': 0,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def buy_token(token_address: str, amount_eth: float, slippage: float = SLIPPAGE_DEFAULT) -> dict:
    """
    Buy a token via Aerodrome Slipstream CL.

    amount_eth is always denominated in WETH (or ETH equivalent).
    For USDC-quoted tokens (e.g. VFY), the WETH value is converted to USDC at market rate.
    Tokens in WATCH_ONLY_TOKENS are skipped with no TX sent (no execution path available).
    """
    slippage = min(slippage, SLIPPAGE_MAX)
    result = {
        'action': 'BUY',
        'token_address': token_address,
        'amount_eth': amount_eth,
        'tx_hash': None,
        'status': 'failed',
        'error': None,
        'ts': datetime.now(timezone.utc).isoformat()
    }

    # Pre-flight: watch-only guard - skip with no TX, no gas wasted
    if token_address.lower() in WATCH_ONLY_TOKENS:
        result['error'] = 'watch-only: no execution path configured for this token'
        result['status'] = 'skipped'
        print(f'[EXECUTE] SKIP {token_address[:10]}...: watch-only token (CRV/ETHFI)')
        return result

    # Pre-flight: unknown token guard
    cfg = SLIPSTREAM_TOKENS.get(token_address.lower())
    if not cfg:
        result['error'] = 'no execution path: token not in SLIPSTREAM_TOKENS'
        result['status'] = 'skipped'
        print(f'[EXECUTE] SKIP {token_address[:10]}...: not in SLIPSTREAM_TOKENS')
        return result

    try:
        w3 = get_web3()
        account = get_account()
        token_cs = Web3.to_checksum_address(token_address)

        # ETH floor check
        eth_balance_eth = float(w3.from_wei(w3.eth.get_balance(account.address), 'ether'))
        if eth_balance_eth < 0.005:
            result['error'] = 'Insufficient ETH for gas'
            return result

        deadline = int(time.time()) + DEADLINE_SECONDS
        quote_cs = Web3.to_checksum_address(cfg['quote'])
        is_weth_quoted = cfg['quote'].lower() == WETH_ADDRESS.lower()

        if is_weth_quoted:
            # Standard path: spend WETH
            amount_in_wei = int(amount_eth * 10 ** WETH_DECIMALS)
            quote_decimals = WETH_DECIMALS
            quote_label = 'WETH'
        else:
            # USDC-quoted token (e.g. VFY): convert ETH amount to USDC
            weth_price_usd = _get_weth_price()
            if weth_price_usd <= 0:
                result['error'] = 'Cannot determine WETH price for USDC conversion'
                return result
            amount_usdc = amount_eth * weth_price_usd
            amount_in_wei = int(amount_usdc * 10 ** USDC_DECIMALS)
            quote_decimals = USDC_DECIMALS
            quote_label = f'USDC (~${amount_usdc:.2f})'

        # Check quote token balance
        quote_contract = w3.eth.contract(address=quote_cs, abi=ERC20_ABI)
        quote_balance = quote_contract.functions.balanceOf(account.address).call()
        if quote_balance < amount_in_wei:
            result['error'] = (f'Insufficient {quote_label}: '
                               f'need {amount_in_wei/10**quote_decimals:.6f}, '
                               f'have {quote_balance/10**quote_decimals:.6f}')
            return result

        print(f'[EXECUTE] Slipstream BUY: {amount_in_wei/10**quote_decimals:.6f} {quote_label}')

        _approve(w3, account, quote_cs,
                 Web3.to_checksum_address(SLIPSTREAM_ROUTER), amount_in_wei)

        tx_hash_hex = _slipstream_buy(w3, account, token_cs, amount_in_wei,
                                      quote_decimals, cfg, slippage, deadline)

        print(f'[EXECUTE] TX sent: {tx_hash_hex}')
        receipt = w3.eth.wait_for_transaction_receipt(
            bytes.fromhex(tx_hash_hex.lstrip('0x')), timeout=60)
        if receipt['status'] == 1:
            result['tx_hash'] = tx_hash_hex
            result['status'] = 'ok'
            print(f'[EXECUTE] BUY confirmed: {tx_hash_hex}')
        else:
            result['error'] = f'TX reverted: {tx_hash_hex}'
            print(f'[EXECUTE] BUY REVERTED: {tx_hash_hex}')

    except Exception as e:
        result['error'] = str(e)
        print(f'[EXECUTE] BUY failed: {e}')

    return result


def sell_token(token_address: str, sell_pct: float = 1.0, slippage: float = SLIPPAGE_DEFAULT) -> dict:
    """
    Sell a token via Aerodrome Slipstream CL.
    Tokens in WATCH_ONLY_TOKENS are skipped - no TX sent.
    """
    slippage = min(slippage, SLIPPAGE_MAX)
    result = {
        'action': 'SELL',
        'token_address': token_address,
        'sell_pct': sell_pct,
        'tx_hash': None,
        'status': 'failed',
        'error': None,
        'ts': datetime.now(timezone.utc).isoformat()
    }

    # Pre-flight: watch-only guard
    if token_address.lower() in WATCH_ONLY_TOKENS:
        result['error'] = 'watch-only: no execution path configured for this token'
        result['status'] = 'skipped'
        print(f'[EXECUTE] SKIP {token_address[:10]}...: watch-only token (CRV/ETHFI)')
        return result

    # Pre-flight: unknown token guard
    cfg = SLIPSTREAM_TOKENS.get(token_address.lower())
    if not cfg:
        result['error'] = 'no execution path: token not in SLIPSTREAM_TOKENS'
        result['status'] = 'skipped'
        print(f'[EXECUTE] SKIP {token_address[:10]}...: not in SLIPSTREAM_TOKENS')
        return result

    try:
        w3 = get_web3()
        account = get_account()
        token_cs = Web3.to_checksum_address(token_address)
        token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
        balance = token_contract.functions.balanceOf(account.address).call()
        amount_in = int(balance * sell_pct)
        if amount_in == 0:
            result['error'] = 'No token balance to sell'
            return result

        deadline = int(time.time()) + DEADLINE_SECONDS
        print(f'[EXECUTE] SELL {token_cs} via Slipstream: {amount_in} tokens -> {cfg["quote"][:10]}...')
        tx_hash_hex = _slipstream_sell(w3, account, token_cs, amount_in,
                                       cfg, slippage, deadline)

        print(f'[EXECUTE] TX sent: {tx_hash_hex}')
        receipt = w3.eth.wait_for_transaction_receipt(
            bytes.fromhex(tx_hash_hex.lstrip('0x')), timeout=60)
        if receipt['status'] == 1:
            result['tx_hash'] = tx_hash_hex
            result['status'] = 'ok'
            result['amount_in'] = amount_in
            print(f'[EXECUTE] SELL confirmed: {tx_hash_hex}')
        else:
            result['error'] = f'TX reverted: {tx_hash_hex}'
            print(f'[EXECUTE] SELL REVERTED: {tx_hash_hex}')

    except Exception as e:
        result['error'] = str(e)
        print(f'[EXECUTE] SELL failed: {e}')

    return result


if __name__ == '__main__':
    # CLI test mode: python3 range_execute.py --check
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check wallet connection and balance only')
    args = parser.parse_args()
    if args.check:
        try:
            w3 = get_web3()
            account = get_account()
            usdc_balance = get_usdc_balance(w3, account.address)
            eth_balance_wei = w3.eth.get_balance(account.address)
            eth_balance_eth = float(w3.from_wei(eth_balance_wei, 'ether'))
            print(f'[CHECK] Connected to Base mainnet')
            print(f'[CHECK] Wallet: {account.address}')
            print(f'[CHECK] ETH Balance: {eth_balance_eth:.6f} ETH')
            print(f'[CHECK] USDC Balance: {usdc_balance:.2f} USDC')
            print(f'[CHECK] Chain ID: {w3.eth.chain_id}')
        except Exception as e:
            print(f'[CHECK] Failed: {e}')
            sys.exit(1)
