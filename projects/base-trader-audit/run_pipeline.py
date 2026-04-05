#!/usr/bin/env python3
"""
run_pipeline.py - end-to-end pipeline orchestrator

Orchestrates the full trading pipeline:
1. Discover smart money tokens
2. Load existing positions
3. For each token: fetch data, calculate SR, score signal
4. Log discovery, signals, and trades
5. Update positions on BUY

DATA DIRECTORY: base_dir/data/ - created if it does not exist.
FOUR DATA FILES - written every run:
  data/positions.json      - current open positions (read + rewrite each run)
  data/signal_log.jsonl    - one appended line per token evaluated
  data/trade_log.jsonl     - written by decision.log_trade() on BUY
  data/discovery_log.jsonl - one appended line per pipeline run

CLI:
  python3 run_pipeline.py
  python3 run_pipeline.py --wallet-balance 50
"""

import json
import os
import sys
import uuid
import argparse
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import fcntl
import psutil
import atexit
import urllib.request
import urllib.error

# Load .env before any Nansen API checks - required when running via launchd/cron
_env_path = Path.home() / ".hermes" / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip().removeprefix('export').strip(), _v.strip().strip('"').strip("'"))

# Startup key check — alert and exit if required env vars are missing
def _startup_key_check():
    required = ['NANSEN_API_KEY', 'BASE_WALLET_PRIVATE_KEY', 'BASE_WALLET_ADDRESS']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        msg = f'🚨 [BASE TRADER] STARTUP FAILED — missing env vars: {", ".join(missing)}\nPipeline will not run. Check ~/.hermes/.env and restart.'
        print(msg)
        bot = os.environ.get("DISCORD_BOT_TOKEN")
        if bot:
            try:
                import requests as _r
                _r.post(
                    f"https://discord.com/api/v10/channels/1488012271044132944/messages",
                    headers={"Authorization": f"Bot {bot}", "Content-Type": "application/json"},
                    json={"content": msg},
                    timeout=15,
                )
            except Exception:
                pass
        sys.exit(1)

_startup_key_check()

# Set up paths
base_dir = Path(__file__).parent.parent
data_dir = base_dir / "data"
os.makedirs(data_dir, exist_ok=True)

# Nansen usage tracking
USAGE_LOG = data_dir / 'nansen_usage_log.jsonl'

# PID lockfile - prevent concurrent pipeline runs
lock_path = data_dir / 'pipeline.lock'
if lock_path.exists():
    try:
        existing_pid = int(lock_path.read_text().strip())
        if psutil.pid_exists(existing_pid):
            print(f'[PIPELINE] Already running (PID {existing_pid}), exiting')
            sys.exit(0)
    except (ValueError, Exception):
        pass  # stale lock, continue
lock_path.write_text(str(os.getpid()))
atexit.register(lambda: lock_path.unlink(missing_ok=True))

# Add scripts/ directory to path so sibling modules can be imported
sys.path.insert(0, str(Path(__file__).parent))

from credit_tracker import get_credits_remaining, log_credit_usage, check_daily_budget
from scanner import get_sm_netflow, get_top_holders, get_screener_data, log_api_error, load_candidate_tokens_base
from gecko_terminal import gt_discover_tokens, gt_get_ohlcv, gt_get_token_info
from support_resistance import calculate_sr
from signal_filter import score_signal
from decision import format_decision, log_trade
from execute import buy_token, sell_token, get_total_balance_usd, get_web3


def get_btc_4h_change() -> float:
    """
    Fetch BTC 4h price change from CoinGecko free API.
    
    Returns:
        float: Percentage change over last 4 hours, or 0.0 on error.
    
    API: GET https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1
    Returns hourly price points for last 24h.
    """
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        prices = data.get('prices', [])
        if len(prices) < 2:
            return 0.0
        
        # prices is list of [timestamp_ms, price_usd]
        # Last entry is current, entry at index -17 is ~4 hours ago (24h = 24*60 = 1440 min, 4h = 240 min, 1440/60 = 24 entries per hour, so 4h = 4 entries... wait, hourly data means 1 entry per hour)
        # 24h of hourly data = 24 entries. 4h ago = index -4 (or len-4)
        current_price = prices[-1][1]
        price_4h_ago = prices[-5][1] if len(prices) >= 5 else prices[0][1]
        
        if price_4h_ago == 0:
            return 0.0
        
        change_pct = ((current_price - price_4h_ago) / price_4h_ago) * 100
        return change_pct
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, IndexError, KeyError, Exception):
        return 0.0


def send_telegram_alert(symbol: str, score: int, amount_usd: float, support: float, resistance: float, stop: float, msg_override: str = None) -> None:
    """
    Send a BUY alert to Discord via Bot API.
    
    Called when score >= 175 and action_taken != 'SKIP_OPEN'.
    msg_override: if provided, send this message instead of the default format.
    """
    message = msg_override if msg_override else (
        f'📈 BUY SIGNAL: {symbol} (score {score})\n'
        f'Entry ~${amount_usd:.0f} @ support {support:.4f}\n'
        f'Target {resistance:.4f} · Stop {stop:.4f}\n'
        f'Executing on-chain now...'
    )
    bot = os.environ.get("DISCORD_BOT_TOKEN")
    channel = "1488012271044132944"
    if bot:
        try:
            import requests as _r
            _r.post(
                f"https://discord.com/api/v10/channels/{channel}/messages",
                headers={"Authorization": f"Bot {bot}", "Content-Type": "application/json"},
                json={"content": message},
                timeout=15,
            )
        except Exception as e:
            print(f"[PIPELINE] Discord alert failed for {symbol}: {e}")


def load_settings():
    """Load settings from config/settings.json."""
    config_path = base_dir / "config" / "settings.json"
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {}


def save_positions(positions: list[dict]) -> None:
    """Save positions to data/positions.json."""
    positions_path = data_dir / "positions.json"
    with open(positions_path, 'w') as f:
        json.dump(positions, f, indent=2)


def write_discovery_log(run_id: str, run_ts: str, discovered_tokens: list[dict]) -> None:
    """Write ONE line to data/discovery_log.jsonl."""
    log_path = data_dir / "discovery_log.jsonl"
    record = {
        "ts": run_ts,
        "run_id": run_id,
        "tokens_found": len(discovered_tokens),
        "tokens": [
            {
                "symbol": t['symbol'],
                "holders_count": t.get('holders_count', 0),
                "volume_24h": t.get('volume_24h', 0)
            }
            for t in discovered_tokens
        ]
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')


def write_signal_log(run_id: str, run_ts: str, scored: dict, token: dict, action_taken: str, tier: int = 1) -> None:
    """Write ONE line to data/signal_log.jsonl."""
    log_path = data_dir / "signal_log.jsonl"
    record = {
        "ts": run_ts,
        "run_id": run_id,
        "symbol": scored['symbol'],
        "address": token['address'],
        "score": scored['total_score'],
        "passes": scored['passes'],
        "disqualified": scored['disqualified'],
        "disqualify_reason": scored['disqualify_reason'],
        "pillars": scored['pillars'],
        "holders_count": token.get('holders_count', 0),
        "volume_24h": token.get('volume_24h', 0),
        "action_taken": action_taken,
        "tier": tier
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')


def write_run_log(run_id: str, run_ts: str, duration_s: float, tokens_scanned: int, trades_executed: int, errors: list, credits_used: int) -> None:
    """Write ONE line to data/run_log.jsonl."""
    log_path = data_dir / "run_log.jsonl"
    record = {
        "ts": run_ts,
        "run_id": run_id,
        "duration_s": duration_s,
        "tokens_scanned": tokens_scanned,
        "trades_executed": trades_executed,
        "errors": errors,
        "credits_used": credits_used
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')


def print_summary_table(results: list[dict]) -> None:
    """Print summary table of all evaluated tokens."""
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"{'symbol':<10} | {'holders':>7} | {'score':>5} | {'passes':>6} | {'action':>12}")
    print("-" * 80)
    for r in results:
        print(f"{r['symbol']:<10} | {r['holders']:>7} | {r['score']:>5} | {str(r['passes']):>6} | {r['action']:>12}")
    print("=" * 80)


def print_trade_details(trades: list[dict]) -> None:
    """Print full trade dict for each passing BUY."""
    if not trades:
        print("No trades executed.")
        return
    print("\nTRADES EXECUTED:")
    print("-" * 40)
    for trade in trades:
        print(json.dumps(trade, indent=2))
    print("-" * 40)


def main():
    """Main pipeline execution."""
    # Parse CLI arguments
    parser = argparse.ArgumentParser(description='End-to-end trading pipeline orchestrator')
    parser.add_argument('--wallet-balance', type=float, default=50.0, help='Wallet balance in USD (default: 50.0)')
    args = parser.parse_args()

    cli_wallet_balance = args.wallet_balance
    btc_change_4h = get_btc_4h_change()
    print(f'[PIPELINE] BTC 4h change: {btc_change_4h:+.1f}%')

    # Generate run ID and timestamp
    run_id = str(uuid.uuid4())[:8]
    run_ts = datetime.utcnow().isoformat() + 'Z'
    start_time = time.time()

    # Budget gate at start of main(), before credits_before call
    ok, msg = check_daily_budget()
    print(f'[CREDITS] {msg}')
    if not ok:
        print('[PIPELINE] Daily credit budget exceeded -- skipping run.')
        return

    # Credit tracking
    credits_before = get_credits_remaining()

    # Low credit warning: only alert if we can confirm credits AND they're low
    if credits_before != -1 and credits_before < 500:
        msg = f'⚠️ [BASE TRADER] Only {credits_before} Nansen credits remaining.'
        print(f'[PIPELINE] {msg}')
        send_telegram_alert(symbol="CREDITS", score=0, amount_usd=0, support=0, resistance=0, stop=0, msg_override=msg)

    # Query on-chain wallet balance
    wallet_balance = cli_wallet_balance
    wallet_address = os.environ.get('BASE_WALLET_ADDRESS', '')
    if wallet_address:
        try:
            w3 = get_web3()
            wallet_balance = get_total_balance_usd(w3, wallet_address)
            print(f'[PIPELINE] On-chain wallet balance: ${wallet_balance:.2f}')
        except Exception as e:
            print(f'[PIPELINE] WARNING: On-chain balance query failed ({e}), using CLI fallback')
            wallet_balance = cli_wallet_balance
    else:
        print(f'[PIPELINE] WARNING: BASE_WALLET_ADDRESS not set, using CLI fallback')
        wallet_balance = cli_wallet_balance

    print(f"[PIPELINE] Run ID: {run_id}")
    print("[PIPELINE] Mode: LIVE")
    print(f"[PIPELINE] Timestamp: {run_ts}")
    print(f"[PIPELINE] Wallet Balance: ${wallet_balance:.2f}")
    print(f"[PIPELINE] BTC Change 4h: {btc_change_4h}%")

    # Get max_enriched_per_run and min_trade_usd from settings (default: 5 and 5.0)
    max_enriched_per_run = load_settings().get('max_enriched_per_run', 5)
    min_trade_usd = load_settings().get('min_trade_usd', 5.0)
    print(f"[PIPELINE] Minimum trade size: ${min_trade_usd:.2f}")

    # Check if wallet is too small for 20% position
    if wallet_balance * 0.20 < min_trade_usd:
        print(f"[PIPELINE] Wallet too small for 20% position (${wallet_balance * 0.20:.2f} < ${min_trade_usd:.2f}), skipping trading")
        sys.exit(0)

    # Step 2: Discover tokens — Nansen SM netflow (primary) + GeckoTerminal (fallback)
    print("[PIPELINE] Discovering tokens via Nansen SM netflow (Base)...")
    discovered_tokens = load_candidate_tokens_base()

    if not discovered_tokens:
        print("[PIPELINE] SM discovery returned 0 — falling back to GeckoTerminal...")
        discovered_tokens = gt_discover_tokens(chain='base', limit=20)

    if not discovered_tokens:
        print("[PIPELINE] No tokens discovered from any source")
        sys.exit(0)

    print(f"[PIPELINE] Discovered {len(discovered_tokens)} tokens")

    # Step 2b: Pre-resolve Aerodrome pools for all discovered tokens (DexScreener, cached)
    try:
        from pool_resolver import resolve_pools
        _addrs = [t['address'] for t in discovered_tokens]
        resolve_pools(_addrs)
        print(f"[PIPELINE] Pool resolution complete — pool_cache.json updated")
    except Exception as _e:
        print(f"[PIPELINE] Pool resolution failed (non-fatal): {_e}")

    # Load blacklist
    _blacklist_file = data_dir / 'blacklist.json'
    try:
        _bl = json.load(open(_blacklist_file))
        blacklisted_addresses = {t['address'].lower() for t in _bl.get('tokens', [])}
        blacklisted_symbols = {t['symbol'].upper() for t in _bl.get('tokens', [])}
    except Exception:
        blacklisted_addresses = set()
        blacklisted_symbols = set()
    print(f"[PIPELINE] Blacklist: {len(blacklisted_symbols)} tokens — {blacklisted_symbols or 'none'}")

    # Build failed-buy cooldown set: skip tokens with a failed BUY in the last 4 hours
    _cooldown_hours = 4
    _cutoff = datetime.now(timezone.utc) - timedelta(hours=_cooldown_hours)
    failed_cooldown_addresses = set()
    try:
        trade_log_path = data_dir / 'trade_log.jsonl'
        with open(trade_log_path) as _tf:
            for _line in _tf:
                try:
                    _t = json.loads(_line)
                    if (_t.get('exec', {}).get('status') == 'failed'
                            and _t.get('event') == 'BUY'):
                        _ts_str = _t.get('exec', {}).get('ts') or _t.get('ts', '')
                        if _ts_str:
                            from datetime import datetime as _dt
                            _ts = _dt.fromisoformat(_ts_str.replace('Z', '+00:00'))
                            if _ts > _cutoff:
                                failed_cooldown_addresses.add(_t.get('address', '').lower())
                except Exception:
                    pass
    except Exception:
        pass
    if failed_cooldown_addresses:
        print(f"[PIPELINE] Failed-buy cooldown active for {len(failed_cooldown_addresses)} addresses")

    # Reconcile positions against on-chain balances
    try:
        from reconcile import reconcile
        positions = reconcile(str(data_dir / 'positions.json'),
                              str(data_dir / 'reconcile_log.jsonl'),
                              os.environ.get('BASE_WALLET_ADDRESS', ''))
        open_symbols = {p['symbol'] for p in positions if p['status'] == 'open'}
        open_addresses = {p['address'].lower() for p in positions if p['status'] == 'open'}
        print(f'[PIPELINE] Reconcile complete: {len([p for p in positions if p["status"]=="open"])} open positions')
    except Exception as e:
        print(f'[PIPELINE] Reconcile failed (non-fatal): {e}')

    # Step 4: Write discovery log
    write_discovery_log(run_id, run_ts, discovered_tokens)
    print(f"[PIPELINE] Discovery log written")

    # Step 5: Process each token
    results = []
    trades = []
    enriched_count = 0  # Counter for enriched evaluations

    for token in discovered_tokens:
        symbol = token['symbol']
        address = token['address']
        print(f"\n[PIPELINE] Processing {symbol} ({address})...")

        # 5a: Get price OHLCV via GeckoTerminal (free, disk-cached daily)
        pool_address = token.get('pool_address', '')
        if not pool_address:
            # Token came from a source without pool_address -- look it up
            info = gt_get_token_info(address)
            pool_address = info.get('top_pool_address', '')
            if pool_address:
                token['pool_address'] = pool_address
        candles = gt_get_ohlcv(pool_address, token_address=address, days=30) if pool_address else []
        # 5b: Calculate support/resistance (scoring handles sr=None gracefully)
        sr = calculate_sr(candles)

        # 5c: Get smart money netflow — pass trader_count + netflow from SM discovery (no extra API call)
        netflow = get_sm_netflow(
            address,
            trader_count=token.get('trader_count'),
            netflow_24h_usd=token.get('netflow_24h_usd')
        )

        # 5d: DEX trades removed -- too expensive. Pass empty dict.
        trades_data = {}

        # 5e: Get top holders
        holders = get_top_holders(address)

        # 5f: Build pool data
        # Primary: liquidity_usd from GeckoTerminal discovery (already fetched, free).
        # Fallback: screener liquidity field from Nansen.
        liquidity = float(token.get('liquidity_usd', 0) or 0)
        if liquidity < 100_000:
            screener = get_screener_data(address)
            liquidity = float(screener.get('liquidity', 0) or 0)
        if liquidity < 100_000:
            liquidity = float(token.get('volume_24h', 0.0) or 0)
        pool = {
            'tvl_usd': liquidity,
            'meets_minimum': liquidity >= 100_000
        }

        # 5g: Build full token dict
        token_full = {
            **token,
            'chain': 'base',
            'min_liquidity_usd': 100_000.0
        }

        # Tier 1: Preliminary scoring with screener-only data
        scored_tier1 = score_signal(
            token_full, sr, netflow, trades_data, holders, pool,
            btc_change_4h=btc_change_4h, list_type='smart_money'
        )

        # Check if token passes Tier 1 (score >= 100)
        if scored_tier1['total_score'] < 100:
            print(f"[PIPELINE] {symbol}: Tier 1 score {scored_tier1['total_score']} < 100, skipping")
            action_taken = 'SKIP_FAIL'
            write_signal_log(run_id, run_ts, scored_tier1, token, action_taken, tier=1)
            results.append({
                'symbol': symbol,
                'holders': token.get('holders_count', 0),
                'score': scored_tier1['total_score'],
                'passes': scored_tier1['passes'],
                'action': action_taken
            })
            continue

        # Tier 2: Enriched scoring with multi-timeframe flow data
        # Check if we've hit the enriched evaluation cap
        if enriched_count >= max_enriched_per_run:
            print(f"[PIPELINE] {symbol}: Enriched cap ({max_enriched_per_run}) reached, using Tier 1 only")
            scored = scored_tier1
            tier = 1
        else:
            # Call get_sm_netflow with enrich=True to get multi-timeframe flow data
            netflow_enriched = get_sm_netflow(
                address, enrich=True,
                trader_count=token.get('trader_count'),
                netflow_24h_usd=token.get('netflow_24h_usd')
            )
            scored = score_signal(
                token_full, sr, netflow_enriched, trades_data, holders, pool,
                btc_change_4h=btc_change_4h, list_type='smart_money'
            )
            tier = 2
            enriched_count += 1
            print(f"[PIPELINE] {symbol}: Tier 2 enriched score {scored['total_score']} (enriched_count={enriched_count}/{max_enriched_per_run})")

        # 5h: Determine action_taken
        if symbol.upper() in blacklisted_symbols or address.lower() in blacklisted_addresses:
            action_taken = 'SKIP_BLACKLIST'
        elif address.lower() in failed_cooldown_addresses:
            action_taken = 'SKIP_COOLDOWN'
        elif symbol in open_symbols or address.lower() in open_addresses:
            action_taken = 'SKIP_OPEN'
        elif scored['passes']:
            action_taken = 'BUY'
        else:
            action_taken = 'SKIP_FAIL'

        # 5i: Write signal log
        write_signal_log(run_id, run_ts, scored, token, action_taken, tier=tier)

        # 5j: Send Telegram BUY alert only if actually executing a BUY
        score = scored['total_score']
        if score >= 175 and action_taken == 'BUY':
            # Format decision FIRST so we have the correct position size for the alert
            trade = format_decision(scored, wallet_balance, pool['tvl_usd'])
            
            # Extract values for the alert message
            sr_summary = scored.get('sr_summary', {})
            support = sr_summary.get('support') or sr.get('support') if sr else None
            resistance = sr_summary.get('resistance') or sr.get('resistance') if sr else None
            current_price = sr.get('current_price') if sr else token.get('price_usd')
            
            # Fall back to current_price when SR is missing (fresh SM tokens)
            if not support:
                support = current_price
            if not resistance:
                resistance = current_price * 1.10 if current_price else 0  # 10% above as rough resistance
            
            # Stop loss: 5% below support
            stop = support * 0.95 if support else 0
            
            # Alert shows actual position size (20% of wallet, not 50% placeholder)
            send_telegram_alert(symbol, score, trade['position_usd'], support, resistance, stop)
            print(f"[PIPELINE] {symbol}: Telegram alert sent")

        # Add to results for summary
        results.append({
            'symbol': symbol,
            'holders': token.get('holders_count', 0),
            'score': scored['total_score'],
            'passes': scored['passes'],
            'action': action_taken
        })

        # 5k: Execute BUY if applicable
        if action_taken == 'BUY':
            print(f"[PIPELINE] {symbol}: BUY signal!")

            # Execute BUY on-chain FIRST — only write position after confirmed
            exec_result = buy_token(address, trade['position_usd'])
            trade['exec'] = exec_result
            log_trade(trade, str(data_dir))

            if exec_result.get('status') == 'ok':
                print(f'[PIPELINE] {symbol}: BUY executed on-chain, tx={exec_result["tx_hash"]}')
                # Only add to positions after on-chain confirmation
                actual_tokens = exec_result.get('token_amount_out', trade['position_usd'] / trade['entry_price'] if trade['entry_price'] else 0)
                positions.append({
                    "symbol": trade['symbol'],
                    "address": address,
                    "opened_at": run_ts,
                    "entry_price": trade['entry_price'],
                    "position_usd": actual_tokens * trade['entry_price'],
                    "token_balance": actual_tokens,
                    "stop_loss": trade['stop_loss'],
                    "target": trade['target'],
                    "score": trade['score'],
                    "status": "open",
                    "thesis": trade['thesis']
                })
                open_symbols.add(trade['symbol'])
                open_addresses.add(address.lower())
                save_positions(positions)
                send_telegram_alert(
                    symbol, score,
                    trade['position_usd'],
                    trade['entry_price'],
                    trade['target'],
                    trade['stop_loss'],
                    msg_override=(
                        f'✅ BUY CONFIRMED: {symbol}\n'
                        f'${trade["position_usd"]:.0f} @ {trade["entry_price"]:.4f}\n'
                        f'Target {trade["target"]:.4f} · Stop {trade["stop_loss"]:.4f}\n'
                        f'tx: {exec_result["tx_hash"][:16]}...\n'
                        f'Verifying wallet balance...'
                    )
                )
            else:
                print(f'[PIPELINE] {symbol}: BUY on-chain FAILED: {exec_result.get("error")}')
                send_telegram_alert(
                    symbol, score, 0, 0, 0, 0,
                    msg_override=f'❌ BUY FAILED: {symbol}\n{exec_result.get("error", "unknown error")}'
                )

            # Add to trades list for summary
            trades.append(trade)

            print(f"[PIPELINE] {symbol}: Trade logged, positions updated")
        else:
            print(f"[PIPELINE] {symbol}: {action_taken} (score={scored['total_score']}, passes={scored['passes']})")

    # Step 6: Print summary table
    print_summary_table(results)

    # Print trade details
    print_trade_details(trades)

    # Credit tracking
    credits_after = get_credits_remaining()
    credits_used = credits_before - credits_after
    log_credit_usage(run_id, credits_before, credits_after)
    if credits_after >= 0:
        print(f'[CREDITS] {credits_before - credits_after} used this run. {credits_after} remaining.')

    # Nansen usage log
    duration_s = round(time.time() - start_time, 1)
    usage_entry = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'run_id': run_id,
        'ts': datetime.now(timezone.utc).isoformat(),
        'tokens_scanned_nansen': len(results),
        'credits_used': credits_used,
        'run_duration_s': duration_s,
    }
    with open(USAGE_LOG, 'a') as f:
        f.write(json.dumps(usage_entry) + '\n')

    # Compute duration and write run log
    duration_s = round(time.time() - start_time, 1)
    confirmed_trades = [t for t in trades if t.get('exec', {}).get('status') == 'ok']
    write_run_log(run_id, run_ts, duration_s, len(results), len(confirmed_trades), [], credits_used)

    # Part A - Wire verifier into pipeline
    import subprocess as _subprocess
    _venv_python = str(Path(__file__).parent.parent / 'venv' / 'bin' / 'python3')
    _verifier = str(Path(__file__).parent / 'verifier.py')
    _result = _subprocess.run([_venv_python, _verifier], capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
    print(_result.stdout)
    if _result.returncode != 0:
        print(f'[PIPELINE] CRITICAL: verifier returned exit {_result.returncode}')

    print(f"\n[PIPELINE] Run {run_id} complete.")
    print(f"[PIPELINE] Total tokens processed: {len(results)}")
    print(f"[PIPELINE] Trades executed: {len(trades)}")
    print(f"[PIPELINE] Enriched evaluations: {enriched_count}/{max_enriched_per_run}")

    sys.exit(0)


if __name__ == '__main__':
    main()
