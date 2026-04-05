#!/usr/bin/env python3
"""
execute.py - Live Aerodrome swap execution

Executes token swaps via Aerodrome Router on Base mainnet.
Called by run_pipeline.py when a BUY signal passes threshold.
"""

import os
import json
import sys
import time
import requests
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

# Constants
# Constants
# Ordered by preference. get_web3() rotates through all options with retry + backoff.
# When 429 hits, it retries with backoff before moving to the next.
BASE_RPC = 'https://mainnet.base.org'
BASE_RPC_FALLBACK = [
    'https://base-mainnet.public.blastapi.io',
    'https://rpc.ankr.com/base',
    'https://base.blockpi.io/rpc/v1',
    'https://base.drpc.org',
    'https://base.lavanet.xyz',
]
ALL_RPCS = [BASE_RPC] + BASE_RPC_FALLBACK
# Max retries per RPC before moving to next
MAX_RETRIES_PER_RPC = 3
# Initial backoff delay seconds
BACKOFF_INITIAL = 2.0
# Backoff multiplier
BACKOFF_MULTIPLIER = 2.0
# Max backoff seconds cap
BACKOFF_MAX = 30.0
AERODROME_ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'
AERODROME_FACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da'
WETH_BASE = '0x4200000000000000000000000000000000000006'
SLIPPAGE_DEFAULT = 0.02   # 2%
SLIPPAGE_MAX = 0.05       # 5% absolute cap
DEADLINE_SECONDS = 120

ROUTER_ABI = [
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
    },
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

# Nansen API for live ETH price (same source used by scanner.py)
NANSEN_API_URL = 'https://api.nansen.ai'
WETH_BASE_ADDRESS = '0x4200000000000000000000000000000000000006'


def get_web3() -> Web3:
    """
    Connect to a Base RPC with per-RPC retry and exponential backoff on 429/5xx.
    Tries each RPC in rotation up to MAX_RETRIES_PER_RPC times, backing off on
    rate-limit errors before moving to the next RPC.
    """
    last_error = None
    for rpc_url in ALL_RPCS:
        delay = BACKOFF_INITIAL
        for attempt in range(MAX_RETRIES_PER_RPC):
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))
                if w3.is_connected():
                    if attempt > 0 or rpc_url != BASE_RPC:
                        print(f'[WEB3] Connected via {rpc_url} (attempt {attempt + 1})')
                    return w3
            except Exception as e:
                err_str = str(e).lower()
                is_rate_limit = ('429' in str(e) or
                                 'rate limit' in err_str or
                                 'too many requests' in err_str)
                if is_rate_limit and attempt < MAX_RETRIES_PER_RPC - 1:
                    print(f'[WEB3] {rpc_url} rate-limited, backing off {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES_PER_RPC})')
                    time.sleep(delay)
                    delay = min(delay * BACKOFF_MULTIPLIER, BACKOFF_MAX)
                    continue
                last_error = e
                break  # non-retryable error or last attempt - try next RPC
        print(f'[WEB3] RPC {rpc_url} failed after {MAX_RETRIES_PER_RPC} attempts: {last_error}')
    raise RuntimeError(f'All Base RPCs unavailable. Last error: {last_error}')


def _get_nonce(w3: Web3, address: str, use_pending: bool = True) -> int:
    """
    Get next nonce for the address.
    
    use_pending=True: include unconfirmed txs in mempool (safe default - prevents
        nonce collisions when there are pending txs from prior runs).
    use_pending=False: confirmed txs only (legacy behavior, use when you need
        the exact confirmed nonce and will handle pending txs separately).
    """
    block_tag = 'pending' if use_pending else 'latest'
    return w3.eth.get_transaction_count(address, block_tag)


def _send_and_wait(w3: Web3, signed_tx) -> dict:
    """
    Send a signed transaction and wait for receipt.
    Returns (tx_hash_hex, receipt). On timeout, returns (tx_hash_hex, None).
    """
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        return tx_hash_hex, receipt
    except Exception:
        # Timed out waiting - tx may be pending in mempool
        return tx_hash_hex, None


def _bump_gas(tx_dict: dict, bump_factor: float = 1.25) -> dict:
    """Return a copy of tx_dict with gas prices bumped by bump_factor."""
    updated = dict(tx_dict)
    if 'maxFeePerGas' in updated:
        updated['maxFeePerGas'] = int(updated['maxFeePerGas'] * bump_factor)
    if 'maxPriorityFeePerGas' in updated:
        updated['maxPriorityFeePerGas'] = int(updated['maxPriorityFeePerGas'] * bump_factor)
    if 'gasPrice' in updated:
        updated['gasPrice'] = int(updated['gasPrice'] * bump_factor)
    return updated


def _wait_for_pending_cleared(w3: Web3, address: str, timeout_seconds: int = 120) -> bool:
    """
    Wait for all pending transactions for address to be mined.
    Returns True when pending count reaches 0, False on timeout.
    """
    from datetime import datetime, timezone
    address = Web3.to_checksum_address(address)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pending_count = w3.eth.get_transaction_count(address, 'pending')
        confirmed_count = w3.eth.get_transaction_count(address, 'latest')
        pending = pending_count - confirmed_count
        if pending == 0:
            print(f'[_NONCE] All pending txs cleared ({confirmed_count} confirmed)')
            return True
        print(f'[_NONCE] Waiting for {pending} pending tx(s) to clear... ')
        time.sleep(10)
    print(f'[_NONCE] Timeout waiting for pending txs to clear - proceeding anyway')
    return False


def get_eth_price_usd(w3: Web3 = None) -> float:
    """
    Fetch live ETH/USD price from multiple sources, best first:
    1. CoinGecko (free, no API key, reliable)
    2. GeckoTerminal ETH/USDC pool on Base (free, no key)
    3. Nansen screener cache from disk (free if pipeline ran recently)
    4. Chainlink ETH/USD Feed on Base (on-chain oracle, no external dependency)
    5. Last resort: conservative fallback (logged as WARNING)
    Always call this before sizing any trade.
    """
    # 1. CoinGecko
    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd',
            timeout=10
        )
        r.raise_for_status()
        price = r.json().get('ethereum', {}).get('usd')
        if price:
            print(f'[EXECUTE] ETH/USD price: ${price:.2f} (CoinGecko)')
            return float(price)
    except Exception as e:
        print(f'[EXECUTE] CoinGecko ETH price failed: {e}')

    # 2. GeckoTerminal ETH/USDC pool on Base
    # Pool: 0x4e5cF1Dd85C6f9F2C5C0f0C1c0C5F0C0C0C0C0C (Base USDC) x ETH
    try:
        r = requests.get(
            'https://api.geckoterminal.com/api/v2/networks/base/pools/0x4e5cF1Dd85C6f9F2C5C0f0C1c0C5F0C0C0C0C0C',
            timeout=10,
            headers={'Accept': 'application/json'}
        )
        if r.status_code == 200:
            data = r.json()
            attrs = data.get('data', {}).get('attributes', {})
            price = attrs.get('base_token_price_usd') or attrs.get('quote_token_price_usd')
            if price and float(price) > 0:
                print(f'[EXECUTE] ETH/USD price: ${float(price):.2f} (GeckoTerminal)')
                return float(price)
    except Exception as e:
        print(f'[EXECUTE] GeckoTerminal ETH price failed: {e}')

    # 3. Nansen screener cache from disk
    try:
        cache_file = Path(__file__).parent.parent / 'data' / 'screener_cache.json'
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            # Cache keys are lowercase addresses; ETH entry may have 'eth' as key
            eth_addresses = [
                '0x4200000000000000000000000000000000000006',  # WETH on Base
                '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',  # ETH (if present)
            ]
            for addr in eth_addresses:
                entry = cache.get(addr.lower(), {})
                price = entry.get('price_usd')
                if price and float(price) > 0:
                    print(f'[EXECUTE] ETH/USD price: ${float(price):.2f} (Nansen cache)')
                    return float(price)
    except Exception as e:
        print(f'[EXECUTE] Nansen cache ETH price failed: {e}')

    # 4. Chainlink ETH/USD Feed on Base (on-chain oracle, no external HTTP)
    try:
        if w3 is None:
            w3 = get_web3()
        chainlink_address = '0x56aa263979AD4789bE8aD17928ED2C7684b0706e'  # Chainlink ETH/USD on Base
        # Minimal ABI for latestAnswer()
        abi = [{'inputs': [], 'name': 'latestAnswer', 'outputs': [{'type': 'int256'}], 'stateMutability': 'view', 'type': 'function'}]
        aggregator = w3.eth.contract(address=chainlink_address, abi=abi)
        price_int = aggregator.functions.latestAnswer().call()
        price = float(price_int) / 1e8  # Chainlink uses 8 decimals
        if price > 0:
            print(f'[EXECUTE] ETH/USD price: ${price:.2f} (Chainlink oracle)')
            return price
    except Exception as e:
        print(f'[EXECUTE] Chainlink ETH price failed: {e}')

    # 5. Last resort fallback
    fallback = 1800.0
    print(f'[EXECUTE] WARNING: All ETH price sources failed - using fallback ${fallback}')
    return fallback


def get_account():
    private_key = os.environ.get('BASE_WALLET_PRIVATE_KEY', '')
    if not private_key:
        raise RuntimeError('BASE_WALLET_PRIVATE_KEY not set in env')
    if not private_key.startswith('0x'):
        private_key = '0x' + private_key
    return Account.from_key(private_key)


def get_total_balance_usd(w3: Web3, address: str) -> float:
    """Get total ETH + WETH balance in USD using live Nansen price.
    Includes native ETH (gas reserve) + WETH token balance (trade capital)."""
    eth_price = get_eth_price_usd()
    address_cs = Web3.to_checksum_address(address)

    # Native ETH balance
    eth_balance_wei = w3.eth.get_balance(address_cs)
    eth_balance = float(w3.from_wei(eth_balance_wei, 'ether'))

    # WETH token balance
    weth_contract = w3.eth.contract(
        address=Web3.to_checksum_address(WETH_BASE), abi=ERC20_ABI)
    weth_balance_wei = weth_contract.functions.balanceOf(address_cs).call()
    weth_balance = float(w3.from_wei(weth_balance_wei, 'ether'))

    total_eth = eth_balance + weth_balance
    return total_eth * eth_price


def buy_token(token_address: str, amount_usd: float, slippage: float = SLIPPAGE_DEFAULT) -> dict:
    """
    Buy a token with WETH via Aerodrome.
    Routes: WETH -> token (volatile pool). No ETH wrap needed.

    Parameters:
        token_address: str - ERC20 token contract on Base
        amount_usd: float - USD value to spend (converted to WETH at current price)
        slippage: float - max acceptable slippage (default 2%)

    Returns:
        dict with tx_hash, token_address, amount_usd, status, error
    """
    slippage = min(slippage, SLIPPAGE_MAX)
    result = {
        'action': 'BUY',
        'token_address': token_address,
        'amount_usd': amount_usd,
        'tx_hash': None,
        'status': 'failed',
        'error': None,
        'ts': datetime.now(timezone.utc).isoformat()
    }
    try:
        w3 = get_web3()
        account = get_account()
        router = w3.eth.contract(
            address=Web3.to_checksum_address(AERODROME_ROUTER),
            abi=ROUTER_ABI
        )
        token_cs = Web3.to_checksum_address(token_address)
        weth_cs = Web3.to_checksum_address(WETH_BASE)
        factory_cs = Web3.to_checksum_address(AERODROME_FACTORY)

        # Get live ETH price and current WETH balance for sizing
        eth_price_usd = get_eth_price_usd()
        weth_contract = w3.eth.contract(address=weth_cs, abi=ERC20_ABI)
        weth_balance_wei = weth_contract.functions.balanceOf(account.address).call()
        weth_balance_eth = float(w3.from_wei(weth_balance_wei, 'ether'))

        amount_eth = amount_usd / eth_price_usd
        amount_wei = w3.to_wei(amount_eth, 'ether')

        # WETH floor: always keep 0.005 WETH as buffer
        WETH_RESERVE = 0.005
        if weth_balance_eth < WETH_RESERVE + amount_eth:
            result['error'] = f'Insufficient WETH: balance={weth_balance_eth:.6f}, need {WETH_RESERVE:.3f} reserve'
            print(f'[EXECUTE] {result["error"]}')
            return result

        # Guard: don't spend more than 95% of WETH balance
        max_spend_wei = int(weth_balance_wei * 0.95)
        if amount_wei > max_spend_wei:
            amount_wei = max_spend_wei
            amount_eth = float(w3.from_wei(amount_wei, 'ether'))
            print(f'[EXECUTE] Capped to {amount_eth:.6f} WETH (95% of balance)')

        if amount_wei == 0:
            result['error'] = 'amount_wei is 0 after sizing'
            return result

        # Approve WETH to router
        # Check for stale pending txs and wait for them to clear before starting
        _wait_for_pending_cleared(w3, account.address, timeout_seconds=60)

        # Nonce from pending so we include any unconfirmed txs in mempool
        nonce = _get_nonce(w3, account.address, use_pending=True)
        approve_tx = weth_contract.functions.approve(
            Web3.to_checksum_address(AERODROME_ROUTER),
            amount_wei
        ).build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 100000,
            'maxFeePerGas': w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
            'chainId': 8453,
        })
        signed_approve = account.sign_transaction(approve_tx)
        approve_hash_hex, approve_receipt = _send_and_wait(w3, signed_approve)
        if approve_receipt is None:
            result['error'] = 'approve tx timed out (pending in mempool)'
            print(f'[EXECUTE] {result["error"]} - will retry on next run')
            return result
        if approve_receipt['status'] != 1:
            result['error'] = f'approve tx reverted: {approve_hash_hex}'
            print(f'[EXECUTE] {result["error"]}')
            return result
        print(f'[EXECUTE] WETH approved: {approve_hash_hex}')

        # Gas cost abort: estimate gas before committing
        try:
            gas_estimate = w3.eth.estimate_gas({
                'from': account.address,
                'to': Web3.to_checksum_address(AERODROME_ROUTER),
                'value': 0,
                'data': b''
            })
            gas_price = w3.eth.gas_price
            gas_cost_eth = gas_estimate * gas_price / 1e18
            gas_cost_usd = gas_cost_eth * eth_price_usd
            print(f'[EXECUTE] Gas estimate: ${gas_cost_usd:.2f}')
            if gas_cost_usd > amount_usd * 0.10:
                result['error'] = f'Gas too high: ${gas_cost_usd:.2f} for ${amount_usd:.2f} trade'
                print(f'[EXECUTE] {result["error"]}')
                return result
        except Exception as e:
            print(f'[EXECUTE] Gas estimation failed (non-fatal): {e}')

        # Build route: WETH -> token (volatile pool)
        routes = [{
            'from': weth_cs,
            'to': token_cs,
            'stable': False,
            'factory': factory_cs
        }]

        # Get expected output
        amounts_out = router.functions.getAmountsOut(amount_wei, routes).call()
        expected_out = amounts_out[-1]
        amount_out_min = int(expected_out * (1 - slippage))

        deadline = int(time.time()) + DEADLINE_SECONDS

        print(f'[EXECUTE] BUY {token_cs}: spend {amount_eth:.6f} WETH, expect {expected_out} tokens, min_out {amount_out_min}')

        # Build swap tx - nonce must be fetched AFTER approve is confirmed
        swap_nonce = _get_nonce(w3, account.address, use_pending=True)
        swap_tx_dict = {
            'from': account.address,
            'value': 0,
            'nonce': swap_nonce,
            'gas': 350000,
            'maxFeePerGas': w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
            'chainId': 8453,
        }
        tx = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount_wei,
            amount_out_min,
            routes,
            Web3.to_checksum_address(account.address),
            deadline
        ).build_transaction(swap_tx_dict)

        signed_swap = account.sign_transaction(tx)

        # Send with gas-bump retry on "replacement transaction underpriced"
        tx_hash_hex = None
        receipt = None
        last_error = None
        for attempt in range(3):
            try:
                tx_hash_hex, receipt = _send_and_wait(w3, signed_swap)
                if receipt is not None:
                    break
                # Timed out - bump gas and retry with same nonce
                print(f'[EXECUTE] Swap tx timed out, bumping gas and retrying (attempt {attempt + 1}/3)')
                swap_tx_dict = _bump_gas(swap_tx_dict, bump_factor=1.25)
                tx = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    amount_wei,
                    amount_out_min,
                    routes,
                    Web3.to_checksum_address(account.address),
                    deadline
                ).build_transaction(swap_tx_dict)
                signed_swap = account.sign_transaction(tx)
            except Exception as e:
                last_error = str(e)
                if 'replacement transaction underpriced' in last_error.lower() or 'nonce too low' in last_error.lower():
                    print(f'[EXECUTE] Nonce conflict or gas too low (attempt {attempt + 1}/3): {last_error}')
                    # Refresh nonce and rebuild
                    swap_tx_dict['nonce'] = _get_nonce(w3, account.address, use_pending=True)
                    swap_tx_dict = _bump_gas(swap_tx_dict, bump_factor=1.25)
                    tx = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                        amount_wei,
                        amount_out_min,
                        routes,
                        Web3.to_checksum_address(account.address),
                        deadline
                    ).build_transaction(swap_tx_dict)
                    signed_swap = account.sign_transaction(tx)
                else:
                    raise

        if receipt is None:
            result['error'] = f'Swap tx pending/unconfirmed after 3 attempts: {last_error or "timeout"}'
            print(f'[EXECUTE] {result["error"]} - TX may have succeeded on-chain, check manually: {tx_hash_hex}')
            result['tx_hash'] = tx_hash_hex
            result['status'] = 'unknown'
            return result

        tx_hash_hex = tx_hash_hex or 'unknown'
        if receipt['status'] == 1:
            result['tx_hash'] = tx_hash_hex
            result['status'] = 'ok'
            result['amount_eth'] = amount_eth
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
    Sell a token back to ETH via Aerodrome.
    Routes: token -> WETH (volatile pool).
    
    Parameters:
        token_address: str - ERC20 token contract on Base
        sell_pct: float - fraction of held balance to sell (default 1.0 = 100%)
        slippage: float - max acceptable slippage (default 2%)
    
    Returns:
        dict with tx_hash, token_address, sell_pct, status, error
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
    try:
        w3 = get_web3()
        account = get_account()
        router = w3.eth.contract(
            address=Web3.to_checksum_address(AERODROME_ROUTER),
            abi=ROUTER_ABI
        )
        token_cs = Web3.to_checksum_address(token_address)
        weth_cs = Web3.to_checksum_address(WETH_BASE)
        factory_cs = Web3.to_checksum_address(AERODROME_FACTORY)

        # Get token balance
        token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
        balance = token_contract.functions.balanceOf(account.address).call()
        amount_in = int(balance * sell_pct)

        if amount_in == 0:
            result['error'] = 'No token balance to sell'
            return result

        # Approve router to spend tokens - wait for any stale pending txs first
        _wait_for_pending_cleared(w3, account.address, timeout_seconds=60)

        nonce = _get_nonce(w3, account.address, use_pending=True)
        approve_tx = token_contract.functions.approve(
            Web3.to_checksum_address(AERODROME_ROUTER),
            amount_in
        ).build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 100000,
            'maxFeePerGas': w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
            'chainId': 8453,
        })
        signed_approve = account.sign_transaction(approve_tx)
        approve_hash_hex, approve_receipt = _send_and_wait(w3, signed_approve)
        if approve_receipt is None:
            result['error'] = 'approve tx timed out (pending in mempool)'
            print(f'[EXECUTE] {result["error"]} - will retry on next run')
            return result
        if approve_receipt['status'] != 1:
            result['error'] = f'approve tx reverted: {approve_hash_hex}'
            print(f'[EXECUTE] {result["error"]}')
            return result
        print(f'[EXECUTE] Approval confirmed: {approve_hash_hex}')

        # Build route: token -> WETH (volatile pool)
        routes = [{
            'from': token_cs,
            'to': weth_cs,
            'stable': False,
            'factory': factory_cs
        }]

        # Get expected output
        amounts_out = router.functions.getAmountsOut(amount_in, routes).call()
        expected_out = amounts_out[-1]
        amount_out_min = int(expected_out * (1 - slippage))

        deadline = int(time.time()) + DEADLINE_SECONDS

        print(f'[EXECUTE] SELL {token_cs}: sell {amount_in} tokens, expect {expected_out} wei ETH, min {amount_out_min}')

        # Nonce fetched AFTER approve is confirmed
        swap_nonce = _get_nonce(w3, account.address, use_pending=True)
        swap_tx_dict = {
            'from': account.address,
            'nonce': swap_nonce,
            'gas': 300000,
            'maxFeePerGas': w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
            'chainId': 8453,
        }
        tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amount_in,
            amount_out_min,
            routes,
            Web3.to_checksum_address(account.address),
            deadline
        ).build_transaction(swap_tx_dict)

        signed_swap = account.sign_transaction(tx)

        # Send with gas-bump retry on replacement/underpriced
        tx_hash_hex = None
        receipt = None
        last_error = None
        for attempt in range(3):
            try:
                tx_hash_hex, receipt = _send_and_wait(w3, signed_swap)
                if receipt is not None:
                    break
                print(f'[EXECUTE] Sell swap tx timed out, bumping gas (attempt {attempt + 1}/3)')
                swap_tx_dict = _bump_gas(swap_tx_dict, bump_factor=1.25)
                tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                    amount_in,
                    amount_out_min,
                    routes,
                    Web3.to_checksum_address(account.address),
                    deadline
                ).build_transaction(swap_tx_dict)
                signed_swap = account.sign_transaction(tx)
            except Exception as e:
                last_error = str(e)
                if 'replacement transaction underpriced' in last_error.lower() or 'nonce too low' in last_error.lower():
                    print(f'[EXECUTE] Nonce conflict (attempt {attempt + 1}/3): {last_error}')
                    swap_tx_dict['nonce'] = _get_nonce(w3, account.address, use_pending=True)
                    swap_tx_dict = _bump_gas(swap_tx_dict, bump_factor=1.25)
                    tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                        amount_in,
                        amount_out_min,
                        routes,
                        Web3.to_checksum_address(account.address),
                        deadline
                    ).build_transaction(swap_tx_dict)
                    signed_swap = account.sign_transaction(tx)
                else:
                    raise

        if receipt is None:
            result['error'] = f'Swap tx pending after 3 attempts: {last_error or "timeout"}'
            print(f'[EXECUTE] {result["error"]} - TX may have succeeded, check: {tx_hash_hex}')
            result['tx_hash'] = tx_hash_hex
            result['status'] = 'unknown'
            return result

        tx_hash_hex = tx_hash_hex or 'unknown'
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
    # CLI test mode: python3 execute.py --check
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check wallet connection and balance only')
    args = parser.parse_args()
    if args.check:
        try:
            w3 = get_web3()
            account = get_account()
            eth_price = get_eth_price_usd(w3)
            bal_wei = w3.eth.get_balance(account.address)
            bal_eth = float(w3.from_wei(bal_wei, 'ether'))
            bal_usd = bal_eth * eth_price
            print(f'[CHECK] Connected to Base mainnet')
            print(f'[CHECK] Wallet: {account.address}')
            print(f'[CHECK] ETH Balance: {bal_eth:.6f} ETH (${bal_usd:.2f} USD)')
            print(f'[CHECK] ETH/USD: ${eth_price:.2f} (Nansen live)')
            print(f'[CHECK] Chain ID: {w3.eth.chain_id}')
        except Exception as e:
            print(f'[CHECK] Failed: {e}')
            sys.exit(1)
