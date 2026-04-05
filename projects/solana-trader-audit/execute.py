import os, json, base64, base58, requests
from pathlib import Path
from datetime import datetime, timezone
import solders.keypair as kp
from solders.transaction import VersionedTransaction
from solders.pubkey import Pubkey

RPC_URL = os.environ.get('HELIUS_RPC_URL', 'https://api.mainnet-beta.solana.com')
SOLANA_PRIVATE_KEY = os.environ.get('SOLANA_PRIVATE_KEY', '')
WALLET = os.environ.get('SOLANA_WALLET_ADDRESS', '')
WSOL = 'So11111111111111111111111111111111111111112'

# Token decimals cache
_TOKEN_DECIMALS = {
    WSOL: 9,
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 6,  # USDC
}
DECIMAL_CACHE = {}

_JUPITER_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
}


def _get_token_decimals(mint: str) -> int:
    """Get decimals for a token mint. Uses cache + on-chain lookup."""
    if mint in _TOKEN_DECIMALS:
        return _TOKEN_DECIMALS[mint]
    if mint in DECIMAL_CACHE:
        return DECIMAL_CACHE[mint]
    try:
        r = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}]
        }, timeout=10)
        result = r.json()
        if 'result' in result and result['result']['value']:
            decimals = result['result']['value']['data']['parsed']['info']['decimals']
            DECIMAL_CACHE[mint] = decimals
            return decimals
    except Exception:
        pass
    DECIMAL_CACHE[mint] = 6
    return 6


def get_sol_balance(wallet_address: str) -> float:
    """Get SOL balance via RPC. Returns 0.0 on error."""
    try:
        r = requests.post(RPC_URL, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'getBalance',
            'params': [wallet_address]
        }, timeout=10)
        lamports = r.json()['result']['value']
        return lamports / 1e9
    except Exception as e:
        print(f'[EXECUTE] getBalance error: {e}')
        return 0.0


def get_token_balance(wallet_address: str, mint_address: str) -> float:
    """Get SPL token balance via Helius RPC. Returns 0.0 on error."""
    try:
        r = requests.post(RPC_URL, json={
            'jsonrpc': '2.0', 'id': 1,
            'method': 'getTokenAccountsByOwner',
            'params': [wallet_address, {'mint': mint_address}, {'encoding': 'jsonParsed'}]
        }, timeout=10)
        accounts = r.json()['result']['value']
        if not accounts:
            return 0.0
        amount = accounts[0]['account']['data']['parsed']['info']['tokenAmount']
        return float(amount.get('uiAmount') or 0)
    except Exception as e:
        print(f'[EXECUTE] getTokenBalance error: {e}')
        return 0.0


def _jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 50) -> dict:
    """Get a Jupiter Ultra quote via GET."""
    params = f"inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}&taker={WALLET}"
    url = f"https://lite-api.jup.ag/ultra/v1/order?{params}"
    r = requests.get(url, headers=_JUPITER_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def _jupiter_execute(request_id: str, signed_tx_b64: str) -> dict:
    """Execute a Jupiter Ultra swap."""
    r = requests.post(
        'https://lite-api.jup.ag/ultra/v1/execute',
        json={"requestId": request_id, "signedTransaction": signed_tx_b64},
        headers=_JUPITER_HEADERS,
        timeout=30
    )
    r.raise_for_status()
    return r.json()


def _sign_transaction(tx_base64: str, wallet: str, private_key: str) -> str:
    """Sign a base64-encoded transaction with the wallet keypair."""
    tx_bytes = base64.b64decode(tx_base64)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    wallet_pubkey = Pubkey.from_string(wallet)
    idx = tx.message.account_keys.index(wallet_pubkey)
    keypair = kp.Keypair.from_bytes(base58.b58decode(private_key))
    signers = list(tx.signatures)
    signers[idx] = keypair  # type: ignore
    signed_tx = VersionedTransaction(tx.message, signers)
    return base64.b64encode(bytes(signed_tx)).decode()


def buy_token(mint: str, symbol: str, amount_sol: float, paper_trade: bool = True) -> dict:
    """Buy `amount_sol` worth of token at `mint`. Returns result dict."""
    if paper_trade:
        print(f'[PAPER] BUY {symbol} {amount_sol:.6f} SOL')
        return {'success': True, 'paper': True, 'tx': None, 'amount_sol': amount_sol, 'tokens_received': amount_sol * 1000}

    try:
        amount_lamports = int(amount_sol * 1e9)
        quote = _jupiter_quote(WSOL, mint, amount_lamports)
        if 'requestId' not in quote:
            return {'success': False, 'error': f'Quote failed: {quote}', 'amount_sol': amount_sol}

        tx_base64 = quote.get('transaction', '')
        if not tx_base64:
            return {'success': False, 'error': 'No transaction in quote', 'amount_sol': amount_sol}

        signed_tx = _sign_transaction(tx_base64, WALLET, SOLANA_PRIVATE_KEY)
        result = _jupiter_execute(quote['requestId'], signed_tx)

        if result.get('status') == 'Success':
            sig = result.get('signature', '')
            out_amount = result.get('totalOutputAmount', '0')
            decimals = _get_token_decimals(mint)
            tokens_received = int(out_amount) / (10 ** decimals)
            print(f'[EXECUTE] BUY {symbol}: {amount_sol} SOL -> {tokens_received} tokens. TX: {sig}')
            return {'success': True, 'paper': False, 'tx': sig, 'amount_sol': amount_sol, 'tokens_received': tokens_received}
        else:
            return {'success': False, 'error': f'Execute failed: {result}', 'amount_sol': amount_sol}
    except Exception as e:
        print(f'[EXECUTE] BUY error for {symbol}: {e}')
        return {'success': False, 'error': str(e), 'amount_sol': amount_sol}


def sell_token(mint: str, symbol: str, token_amount: float, paper_trade: bool = True) -> dict:
    """Sell `token_amount` tokens back to SOL."""
    if paper_trade:
        print(f'[PAPER] SELL {symbol} {token_amount:.4f} tokens')
        return {'success': True, 'paper': True, 'tx': None}

    try:
        decimals = _get_token_decimals(mint)
        amount_raw = int(token_amount * (10 ** decimals))
        quote = _jupiter_quote(mint, WSOL, amount_raw)
        if 'requestId' not in quote:
            return {'success': False, 'error': f'Quote failed: {quote}'}

        tx_base64 = quote.get('transaction', '')
        if not tx_base64:
            return {'success': False, 'error': 'No transaction in quote'}

        signed_tx = _sign_transaction(tx_base64, WALLET, SOLANA_PRIVATE_KEY)
        result = _jupiter_execute(quote['requestId'], signed_tx)

        if result.get('status') == 'Success':
            sig = result.get('signature', '')
            sol_received = int(result.get('totalOutputAmount', 0)) / 1e9
            print(f'[EXECUTE] SELL {symbol}: {token_amount} tokens -> {sol_received:.6f} SOL. TX: {sig}')
            return {'success': True, 'paper': False, 'tx': sig}
        else:
            return {'success': False, 'error': f'Execute failed: {result}'}
    except Exception as e:
        print(f'[EXECUTE] SELL error for {symbol}: {e}')
        return {'success': False, 'error': str(e)}
