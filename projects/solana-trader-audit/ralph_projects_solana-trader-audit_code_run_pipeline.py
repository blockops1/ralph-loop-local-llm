#!/usr/bin/env python3
"""run_pipeline.py - Solana SM trader pipeline orchestrator"""
import sys, os, json, uuid, time
import yaml
from pathlib import Path
from datetime import datetime, timezone

# Load .env
ENV_FILE = Path.home() / '.hermes' / '.env'
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip().removeprefix('export').strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(Path(__file__).parent))

from scanner import load_candidate_tokens, get_flow_intelligence, get_sol_price_usd, get_btc_5h_change
from signal_filter import score_signal
from decision import format_decision, log_trade, load_blacklist, is_on_cooldown, set_cooldown
from execute import buy_token
from exit_monitor import check_exits
from positions import load_positions, save_positions, add_position
import requests

SCRIPTS_DIR = Path(__file__).parent
BASE_DIR = SCRIPTS_DIR.parent
DATA_DIR = BASE_DIR / 'data'
USAGE_LOG = DATA_DIR / 'nansen_usage_log.jsonl'
CONFIG_FILE = BASE_DIR / 'config' / 'config.yaml'
SIGNAL_LOG = DATA_DIR / 'signal_log.jsonl'
RUN_LOG = DATA_DIR / 'run_log.jsonl'
CREDIT_LOG = DATA_DIR / 'credit_log.jsonl'

def notify(msg: str):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    group_id = '-5264050975'  # Trading group only
    if token and group_id:
        try:
            requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': group_id, 'text': msg}, timeout=10)
        except Exception:
            pass

def _startup_key_check():
    """Alert and exit if required env vars are missing. Runs once at startup."""
    required = ['NANSEN_API_KEY', 'SOLANA_PRIVATE_KEY', 'SOLANA_WALLET_ADDRESS', 'TELEGRAM_BOT_TOKEN']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        msg = f'🚨 [SOLANA TRADER] STARTUP FAILED — missing env vars: {", ".join(missing)}\nPipeline will not run. Check ~/.hermes/.env and restart.'
        print(msg)
        notify(msg)
        sys.exit(1)

_startup_key_check()

def log_signal(entry: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')

def log_run(run_id, duration_s, tokens_scanned, trades, errors):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUN_LOG, 'a') as f:
        f.write(json.dumps({'ts': datetime.now(timezone.utc).isoformat(),
                             'run_id': run_id, 'duration_s': duration_s,
                             'tokens_scanned': tokens_scanned,
                             'trades_executed': trades, 'errors': errors}) + '\n')

def get_nansen_credits() -> int:
    try:
        r = requests.get('https://api.nansen.ai/api/v1/account',
                         headers={'apikey': os.environ['NANSEN_API_KEY']}, timeout=10)
        return r.json().get('credits_remaining', -1)
    except Exception:
        return -1

def main():
    run_id = str(uuid.uuid4())[:8]
    start = time.time()
    errors = []
    trades_executed = 0

    config = yaml.safe_load(CONFIG_FILE.read_text())
    paper = config.get('paper_trade', True)
    max_pos = config.get('max_positions', 6)

    print(f'[PIPELINE] Solana trader run {run_id} | paper={paper}')

    credits_before = get_nansen_credits()

    sol_price = get_sol_price_usd()
    print(f'[PIPELINE] SOL price: ${sol_price:.2f}')

    # Exit monitor
    positions = load_positions()
    open_positions = [p for p in positions if p.get('status') in ('open', 'partial')]
    try:
        exits = check_exits(open_positions, config, paper_trade=paper)
        for ex in exits:
            notify(f'[SOLANA] {ex["action"]} {ex["symbol"]} - {ex["reason"]}')
        save_positions(positions)
    except Exception as e:
        errors.append(f'exit_monitor: {e}')
        print(f'[PIPELINE] Exit monitor error: {e}')

    # BTC kill switch
    btc_change = get_btc_5h_change()
    if btc_change < config.get('btc_kill_switch_pct', -3.0):
        print(f'[PIPELINE] BTC kill switch: {btc_change:.1f}% - skipping buys')
        log_run(run_id, time.time()-start, 0, 0, errors)
        return

    # Check credit hard cap
    if credits_before != -1 and credits_before < 500:
        print(f'[PIPELINE] Credit guard: {credits_before} remaining - skip flow-intel')
        notify(f'[SOLANA] WARNING: only {credits_before} Nansen credits remaining')
        log_run(run_id, time.time()-start, 0, 0, errors)
        return

    positions = load_positions()
    open_count = sum(1 for p in positions if p.get('status') in ('open', 'partial'))
    if open_count >= max_pos:
        print(f'[PIPELINE] Max positions reached ({open_count}/{max_pos})')
        log_run(run_id, time.time()-start, 0, 0, errors)
        return

    blacklist = load_blacklist()
    candidates = load_candidate_tokens(config)
    tokens_scanned = len(candidates)

    for token in candidates:
        symbol = token['symbol']
        address = token['address']

        if symbol in blacklist:
            print(f'[PIPELINE] {symbol} blacklisted - skip')
            continue
        if is_on_cooldown(symbol):
            print(f'[PIPELINE] {symbol} on cooldown - skip')
            continue
        from positions import get_position
        if get_position(symbol) and get_position(symbol).get('status') in ('open', 'partial'):
            print(f'[PIPELINE] {symbol} already in position - skip')
            continue

        # Use the netflow data already in the candidate (no per-token API call needed)
        flow_intel = {
            'trader_count': token.get('trader_count', 0),
            'net_flow_24h_usd': token.get('netflow_24h_usd', 0),
            'price_usd': token.get('price_usd', 0),
        }
        scored = score_signal(token, flow_intel, btc_change, config)
        log_signal({'ts': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
                    'symbol': symbol, 'address': address, 'score': scored['total_score'],
                    'passes': scored['passes'], 'reason': scored.get('disqualified_reason')})

        if not scored['passes']:
            print(f'[PIPELINE] {symbol} score={scored["total_score"]} - {scored.get("disqualified_reason","")}')
            continue

        open_count = sum(1 for p in load_positions() if p.get('status') in ('open', 'partial'))
        if open_count >= max_pos:
            print(f'[PIPELINE] Max positions reached - done')
            break

        decision = format_decision(scored, sol_price, config)
        print(f'[PIPELINE] BUY signal: {symbol} score={scored["total_score"]} entry_sol={decision["entry_price_sol"]:.8f}')
        notify(f'[SOLANA] PRE-BUY: {symbol} | Score {scored["total_score"]}/400 | Entry {decision["entry_price_sol"]:.8f} SOL | Stop -{config["stop_loss_pct"]*100:.0f}%')

        result = buy_token(address, symbol, decision['amount_sol'], paper_trade=paper)
        if result.get('success'):
            decision['tx_hash'] = result.get('tx')
            decision['token_balance'] = result.get('tokens_received', decision['amount_sol'] * 1000)
            add_position(decision)
            log_trade({**decision, 'action': 'BUY', 'result': 'success'})
            trades_executed += 1
            notify(f'[SOLANA] BOUGHT {symbol} | {decision["amount_sol"]:.4f} SOL | TX: {result.get("tx","paper")}')
        else:
            set_cooldown(symbol)
            errors.append(f'buy_failed:{symbol}')
            print(f'[PIPELINE] BUY failed for {symbol}: {result.get("error")}')

    credits_after = get_nansen_credits()
    if credits_before != -1 and credits_after != -1:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CREDIT_LOG, 'a') as f:
            f.write(json.dumps({'ts': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
                                 'credits_before': credits_before, 'credits_after': credits_after,
                                 'credits_used': credits_before - credits_after}) + '\n')
        used = credits_before - credits_after
        if used > 0:
            print(f'[PIPELINE] Nansen credits used this run: {used} (remaining: {credits_after})')

    # Nansen usage log
    usage_entry = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'run_id': run_id,
        'ts': datetime.now(timezone.utc).isoformat(),
        'tokens_scanned_nansen': tokens_scanned,
        'credits_used': used if 'used' in dir() else -1,
        'run_duration_s': round(time.time() - start, 1),
    }
    with open(USAGE_LOG, 'a') as f:
        f.write(json.dumps(usage_entry) + '\n')

    log_run(run_id, round(time.time()-start, 1), tokens_scanned, trades_executed, errors)
    print(f'[PIPELINE] Complete: {trades_executed} trades, {tokens_scanned} scanned, {round(time.time()-start,1)}s')

if __name__ == '__main__':
    main()
