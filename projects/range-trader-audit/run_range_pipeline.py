import sys
import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone

# Load .env — required when running via launchd/cron
_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _, _v = _line.partition('=')
            os.environ.setdefault(_k.strip().lstrip('export').strip(), _v.strip().strip('"').strip("'"))

# Startup key check — alert and exit if required env vars are missing
def _startup_key_check():
    import requests
    required = ['METAMASK_WALLET_PRIVATE_KEY', 'METAMASK_WALLET_ADDRESS', 'TELEGRAM_BOT_TOKEN']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        msg = f'🚨 [RANGE TRADER] STARTUP FAILED — missing env vars: {", ".join(missing)}\nPipeline will not run. Check ~/.hermes/.env and restart.'
        print(msg)
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        group_id = '-5264050975'  # Trading group
        if token and group_id:
            try:
                requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              json={'chat_id': group_id, 'text': msg}, timeout=10)
            except Exception:
                pass
        sys.exit(1)

_startup_key_check()

sys.path.insert(0, str(Path(__file__).parent))

from range_scanner import scan_all
from range_signal_filter import filter_signals
from range_execute import buy_token, sell_token, get_usdc_balance, get_account, ERC20_ABI
from range_positions import load_positions, add_position, remove_position
from range_reconcile import reconcile as range_reconcile
from web3 import Web3

MAX_POSITIONS = 4
POSITION_SIZE_PCT = 0.25  # 25% of ETH balance per position
MIN_ETH_TRADE = 0.003
BASE_RPC = 'https://mainnet.base.org'
USDC_ADDRESS = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
DATA_DIR = Path(__file__).parent.parent / 'data'
TRADE_LOG = DATA_DIR / 'range_trade_log.jsonl'
RUN_LOG = DATA_DIR / 'range_run_log.jsonl'
CREDIT_LOG = DATA_DIR / 'range_credit_log.jsonl'
LOCKFILE = DATA_DIR / 'range_pipeline.lock'
BTC_KILL_SWITCH_PCT = -3.0


def write_trade_log(entry: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def write_run_log(run_id, duration_s, tokens_scanned, trades_executed, errors, credits_used):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'run_id': run_id,
        'duration_s': duration_s,
        'tokens_scanned': tokens_scanned,
        'trades_executed': trades_executed,
        'errors': errors,
        'credits_used': credits_used
    }
    with open(RUN_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def get_eth_balance(w3, address: str) -> float:
    """Get ETH balance in ETH float (divides by 1e18)."""
    balance_wei = w3.eth.get_balance(address)
    return float(balance_wei) / 1e18


def get_btc_4h_change() -> float:
    """Get BTC price change over the last 4 hours from CoinGecko API.
    
    Returns:
        float: Percentage change (negative if dropped), 0.0 on error
    """
    import requests
    try:
        resp = requests.get(
            'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
            params={'vs_currency': 'usd', 'days': 1},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        prices = data.get('prices', [])
        if len(prices) < 2:
            return 0.0
        # prices are [timestamp, price] pairs, sorted ascending
        # Get last price (current) and price from ~4h ago (index ~-480)
        current_price = prices[-1][1]
        # 1 day = 1440 minutes = 86400 seconds
        # 4 hours = 240 minutes = 14400 seconds
        # With 1-hour data points, ~4h ago is index -4
        # But CoinGecko returns 1-hour data for 1-day range, so we need to find ~4h ago
        # prices[-1] is now, prices[-5] is ~4h ago (5 hours back)
        if len(prices) >= 5:
            btc_4h_ago = prices[-5][1]
        else:
            # Fallback: use first price if not enough data
            btc_4h_ago = prices[0][1]
        if btc_4h_ago == 0:
            return 0.0
        change_pct = ((current_price - btc_4h_ago) / btc_4h_ago) * 100
        return change_pct
    except Exception:
        return 0.0


def main():
    import time
    import json
    from datetime import datetime, timezone

    run_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    start_time = time.time()
    trades_executed = 0
    errors = []

    w3 = Web3(Web3.HTTPProvider(BASE_RPC, request_kwargs={'timeout': 30}))
    if not w3.is_connected():
        print('[PIPELINE] Cannot connect to Base RPC')
        sys.exit(1)

    # Reconcile positions at startup (non-fatal)
    try:
        print('[PIPELINE] Running reconciliation...')
        base_dir = Path(__file__).parent.parent
        positions_path = str(base_dir / 'data' / 'range_positions.json')
        log_path = str(base_dir / 'data' / 'range_reconcile_log.jsonl')
        wallet = os.environ.get('METAMASK_WALLET_ADDRESS', '')
        if wallet:
            range_reconcile(positions_path, log_path, wallet)
        else:
            print('[PIPELINE] Warning: METAMASK_WALLET_ADDRESS not set, skipping reconcile')
    except Exception as e:
        print(f'[PIPELINE] Warning: reconcile failed: {e}, continuing anyway')

    # Scan
    print('[PIPELINE] Scanning tokens...')
    scan_results = scan_all()
    tokens_scanned = len([r for r in scan_results if not r.get('error')])

    # Load positions
    positions = load_positions()

    # Filter
    signals = filter_signals(scan_results, positions)
    print(f'[PIPELINE] Signals: {len(signals)}')

    # BTC macro kill switch - check after scan but before filtering
    btc_change = get_btc_4h_change()
    if btc_change < BTC_KILL_SWITCH_PCT:
        print(f'[PIPELINE] BTC macro kill switch: {btc_change:.1f}% in 4h - skipping BUY signals')
        # Filter out BUY signals, keep SELL signals
        signals = [s for s in signals if s['action'] == 'SELL']
        # Log kill switch activation
        write_run_log(run_id, 0, tokens_scanned, 0, [], 0)

    # Execute SELL signals first (free up USDC before buying)
    for sig in [s for s in signals if s['action'] == 'SELL']:
        print(f'[PIPELINE] SELL {sig["symbol"]} reason={sig["reason"]}')
        result = sell_token(sig['address'])
        log_entry = {**sig, **result, 'run_id': run_id}
        write_trade_log(log_entry)
        if result['status'] == 'ok':
            remove_position(sig['symbol'])
            trades_executed += 1
        else:
            errors.append(f'SELL {sig["symbol"]}: {result["error"]}')

    # Reload positions after sells
    positions = load_positions()

    # Execute BUY signals
    for sig in [s for s in signals if s['action'] == 'BUY']:
        if len(positions) >= MAX_POSITIONS:
            print(f'[PIPELINE] Max positions ({MAX_POSITIONS}) reached, skipping {sig["symbol"]}')
            break
        eth_balance = get_eth_balance(w3, os.environ.get('METAMASK_WALLET_ADDRESS', ''))
        tradeable_eth = eth_balance - 0.005  # Keep 0.005 ETH reserved for gas
        amount_eth = tradeable_eth * POSITION_SIZE_PCT
        if amount_eth < MIN_ETH_TRADE:
            print(f'[PIPELINE] ETH too low ({eth_balance:.4f}), skipping')
            break
        print(f'[PIPELINE] BUY {sig["symbol"]} {amount_eth:.4f} ETH')
        result = buy_token(sig['address'], amount_eth)
        log_entry = {**sig, **result, 'run_id': run_id}
        write_trade_log(log_entry)
        if result['status'] == 'ok':
            # Calculate tokens_held from the buy result
            # Fetch actual token balance from chain
            tok = w3.eth.contract(address=Web3.to_checksum_address(sig['address']), abi=ERC20_ABI)
            decimals = tok.functions.decimals().call()
            raw_bal = tok.functions.balanceOf(Web3.to_checksum_address(os.environ.get('METAMASK_WALLET_ADDRESS', ''))).call()
            tokens_held = raw_bal / (10 ** decimals)
            # entry_usdc: USD value spent (for PnL reporting)
            # For WETH-quoted tokens: amount_eth * ETH price
            # For USDC-quoted tokens (VFY): amount was already in USDC terms
            from range_execute import WETH_ADDRESS, _get_weth_price
            from range_execute import SLIPSTREAM_TOKENS as _ST
            _cfg = _ST.get(sig['address'].lower(), {})
            _is_usdc_quoted = _cfg.get('quote', '').lower() != WETH_ADDRESS.lower()
            if _is_usdc_quoted:
                entry_usdc = amount_eth * _get_weth_price()  # ETH equiv → USD
            else:
                entry_usdc = amount_eth * _get_weth_price()
            pos = {
                'symbol': sig['symbol'],
                'address': sig['address'].lower(),
                'entry_price': sig['price_usd'],
                'entry_eth': amount_eth,
                'entry_usdc': round(entry_usdc, 2),
                'stop_loss': sig['stop_loss'],
                'exit_price': sig['exit_price'],
                'score': sig['score'],
                'support': sig['support'],
                'resistance': sig['resistance'],
                'tokens_held': tokens_held,
                'entry_ts': datetime.now(timezone.utc).isoformat(),
                'tx_hash': result.get('tx_hash', '')
            }
            add_position(pos)
            positions = load_positions()
            trades_executed += 1
        else:
            errors.append(f'BUY {sig["symbol"]}: {result["error"]}')

    duration_s = round(time.time() - start_time, 1)
    write_run_log(run_id, duration_s, tokens_scanned, trades_executed, errors, credits_used=0)
    print(f'[PIPELINE] Done. trades={trades_executed} errors={len(errors)} duration={duration_s}s')


if __name__ == '__main__':
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip())
            import psutil
            if psutil.pid_exists(pid):
                print(f'[PIPELINE] Already running (PID {pid})')
                sys.exit(0)
        except Exception:
            pass
        LOCKFILE.unlink(missing_ok=True)
    LOCKFILE.write_text(str(os.getpid()))
    try:
        main()
    finally:
        LOCKFILE.unlink(missing_ok=True)
