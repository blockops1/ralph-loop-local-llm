#!/usr/bin/env python3
"""
swap_usdc_to_weth.py — One-time swap of USDC → WETH via Aerodrome Slipstream on Base.

Adapted directly from _slipstream_buy() in range_execute.py — same router, same
exactInputSingle call pattern, with USDC as tokenIn instead of WETH.

Usage:
    python3 scripts/swap_usdc_to_weth.py --amount 500 [--dry-run]
"""
import argparse, os, sys, time
from pathlib import Path
from web3 import Web3

# ── Load .env ────────────────────────────────────────────────────────────────
_env = Path.home() / '.hermes' / '.env'
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip().removeprefix('export').strip(),
                                  v.strip().strip('"').strip("'"))

# ── Constants (from range_execute.py) ────────────────────────────────────────
WALLET_ADDRESS  = os.environ['METAMASK_WALLET_ADDRESS']
PRIVATE_KEY     = os.environ['METAMASK_WALLET_PRIVATE_KEY']

SLIPSTREAM_ROUTER = '0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5'
USDC_ADDRESS      = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
WETH_ADDRESS      = '0x4200000000000000000000000000000000000006'
USDC_DECIMALS     = 6
WETH_DECIMALS     = 18
TICK_SPACING      = 100    # USDC/WETH Slipstream pool tick spacing
SLIPPAGE          = 0.01   # 1% — matches range_execute.py default
GAS_RESERVE_ETH   = 0.005

SLIPSTREAM_ROUTER_ABI = [
    {'name': 'exactInputSingle', 'type': 'function', 'stateMutability': 'payable',
     'inputs': [{'name': 'params', 'type': 'tuple', 'components': [
         {'name': 'tokenIn',           'type': 'address'},
         {'name': 'tokenOut',          'type': 'address'},
         {'name': 'tickSpacing',       'type': 'int24'},
         {'name': 'recipient',         'type': 'address'},
         {'name': 'deadline',          'type': 'uint256'},
         {'name': 'amountIn',          'type': 'uint256'},
         {'name': 'amountOutMinimum',  'type': 'uint256'},
         {'name': 'sqrtPriceLimitX96', 'type': 'uint160'},
     ]}],
     'outputs': [{'name': 'amountOut', 'type': 'uint256'}]},
]

ERC20_ABI = [
    {'name': 'approve',   'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [{'name': 'spender', 'type': 'address'}, {'name': 'amount', 'type': 'uint256'}],
     'outputs': [{'name': '', 'type': 'bool'}]},
    {'name': 'allowance', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'owner', 'type': 'address'}, {'name': 'spender', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--amount', type=float, required=True, help='USDC amount to swap')
    parser.add_argument('--dry-run', action='store_true', help='Preview only, no tx submitted')
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider('https://mainnet.base.org'))
    wallet_cs  = Web3.to_checksum_address(WALLET_ADDRESS)
    account    = w3.eth.account.from_key(PRIVATE_KEY)
    router_cs  = Web3.to_checksum_address(SLIPSTREAM_ROUTER)
    usdc_cs    = Web3.to_checksum_address(USDC_ADDRESS)
    weth_cs    = Web3.to_checksum_address(WETH_ADDRESS)

    # Guard: ETH for gas
    eth_bal = float(w3.from_wei(w3.eth.get_balance(wallet_cs), 'ether'))
    print(f'ETH balance:  {eth_bal:.6f}')
    if eth_bal < GAS_RESERVE_ETH:
        print(f'ERROR: ETH too low for gas ({eth_bal:.6f} < {GAS_RESERVE_ETH})')
        sys.exit(1)

    # Guard: USDC balance
    usdc = w3.eth.contract(address=usdc_cs, abi=ERC20_ABI)
    usdc_bal = usdc.functions.balanceOf(wallet_cs).call() / 10**USDC_DECIMALS
    print(f'USDC balance: {usdc_bal:.2f}')
    if usdc_bal < args.amount:
        print(f'ERROR: insufficient USDC ({usdc_bal:.2f} < {args.amount})')
        sys.exit(1)

    amount_in_wei = int(args.amount * 10**USDC_DECIMALS)

    # Estimate WETH out using current ETH price (~$2,130) — no API call needed
    # amountOutMinimum = amount_usdc / eth_price * (1 - slippage)
    # We use a conservative live estimate via the Quoter if available, else approximation
    try:
        QUOTER = '0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a'
        QUOTER_ABI = [{'name': 'quoteExactInputSingle', 'type': 'function', 'stateMutability': 'nonpayable',
            'inputs': [{'name': 'params', 'type': 'tuple', 'components': [
                {'name': 'tokenIn', 'type': 'address'}, {'name': 'tokenOut', 'type': 'address'},
                {'name': 'amountIn', 'type': 'uint256'}, {'name': 'fee', 'type': 'uint24'},
                {'name': 'sqrtPriceLimitX96', 'type': 'uint160'}
            ]}], 'outputs': [
                {'name': 'amountOut', 'type': 'uint256'}, {'name': 'sqrtPriceX96After', 'type': 'uint160'},
                {'name': 'initializedTicksCrossed', 'type': 'uint32'}, {'name': 'gasEstimate', 'type': 'uint256'}
            ]}]
        quoter = w3.eth.contract(address=Web3.to_checksum_address(QUOTER), abi=QUOTER_ABI)
        result = quoter.functions.quoteExactInputSingle(
            {'tokenIn': usdc_cs, 'tokenOut': weth_cs, 'amountIn': amount_in_wei, 'fee': 500, 'sqrtPriceLimitX96': 0}
        ).call()
        weth_out_wei = result[0]
    except Exception:
        weth_out_wei = int(args.amount / 2135 * 10**WETH_DECIMALS)  # fallback

    weth_out = weth_out_wei / 10**WETH_DECIMALS
    weth_out_min_wei = int(weth_out_wei * (1 - SLIPPAGE))
    eth_price = args.amount / weth_out if weth_out > 0 else 0

    print(f'\nSwap preview (Aerodrome Slipstream, tick_spacing={TICK_SPACING}):')
    print(f'  IN:  {args.amount:.2f} USDC')
    print(f'  OUT: ~{weth_out:.6f} WETH  (~${eth_price:,.0f}/ETH implied)')
    print(f'  Min: {weth_out_min_wei/10**WETH_DECIMALS:.6f} WETH (1% slippage)')

    if args.dry_run:
        print('\n[DRY RUN] No transaction submitted.')
        return

    # ── Approve USDC for Slipstream router ───────────────────────────────────
    current_allowance = usdc.functions.allowance(wallet_cs, router_cs).call()
    if current_allowance < amount_in_wei:
        print('\nApproving USDC for Slipstream router...')
        gas_price = w3.eth.gas_price
        approve_tx = usdc.functions.approve(router_cs, amount_in_wei).build_transaction({
            'from': wallet_cs,
            'nonce': w3.eth.get_transaction_count(wallet_cs, 'latest'),
            'gas': 80000,
            'maxFeePerGas': gas_price * 2,
            'maxPriorityFeePerGas': w3.to_wei('0.01', 'gwei'),
            'chainId': 8453,
        })
        signed = account.sign_transaction(approve_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            print(f'ERROR: Approve failed. tx={tx_hash.hex()}')
            sys.exit(1)
        print(f'  Approve tx: {tx_hash.hex()}')
        time.sleep(3)

    # ── exactInputSingle: USDC → WETH via Slipstream ─────────────────────────
    print('\nExecuting swap...')
    gas_price = w3.eth.gas_price
    deadline = int(time.time()) + 300
    router = w3.eth.contract(address=router_cs, abi=SLIPSTREAM_ROUTER_ABI)

    swap_tx = router.functions.exactInputSingle({
        'tokenIn':           usdc_cs,
        'tokenOut':          weth_cs,
        'tickSpacing':       TICK_SPACING,
        'recipient':         wallet_cs,
        'deadline':          deadline,
        'amountIn':          amount_in_wei,
        'amountOutMinimum':  weth_out_min_wei,
        'sqrtPriceLimitX96': 0,
    }).build_transaction({
        'from':                  wallet_cs,
        'nonce':                 w3.eth.get_transaction_count(wallet_cs, 'latest'),
        'gas':                   500000,
        'maxFeePerGas':          gas_price * 2,
        'maxPriorityFeePerGas':  w3.to_wei('0.01', 'gwei'),
        'chainId':               8453,
        'value':                 0,
    })
    signed = account.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f'  Tx submitted: {tx_hash.hex()}')

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        print(f'\n✅ SWAP COMPLETE')
        print(f'  Tx:    {tx_hash.hex()}')
        print(f'  Block: {receipt.blockNumber}')
        print(f'  Gas:   {receipt.gasUsed}')
    else:
        print(f'\n❌ SWAP FAILED — tx={tx_hash.hex()}')
        sys.exit(1)


if __name__ == '__main__':
    main()
