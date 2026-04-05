#!/usr/bin/env python3
"""
daily_summary.py — Solana Trader daily HTML email report.
Covers: wallet balance, open positions, yesterday's trades, pipeline run stats.
"""
import os, json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Load .env
_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip().replace('export ', '').strip(), _v.strip())

DATA_DIR = Path(__file__).parent.parent / 'data'

HELIUS_RPC = os.environ.get('HELIUS_RPC_URL', 'https://api.mainnet-beta.solana.com')
WALLET = os.environ.get('SOLANA_WALLET_ADDRESS', '')


def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def get_sol_balance() -> float:
    try:
        import requests
        r = requests.post(HELIUS_RPC, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'getBalance', 'params': [WALLET]
        }, timeout=10)
        return r.json()['result']['value'] / 1e9
    except Exception as e:
        print(f'[WARN] SOL balance error: {e}', file=sys.stderr)
        return 0.0


_SCREENER_CACHE_FILE = DATA_DIR / 'solana_screener_cache.json'
_SOL_MINT = 'So11111111111111111111111111111111111111112'

def _load_screener_cache() -> dict:
    try:
        return json.load(open(_SCREENER_CACHE_FILE))
    except Exception:
        return {}


def get_sol_price_usd() -> float:
    """SOL price: screener cache first, then live DexScreener fallback."""
    cache = _load_screener_cache()
    sol = cache.get(_SOL_MINT) or cache.get(_SOL_MINT.lower())
    if sol and sol.get('price_usd', 0) > 0:
        return float(sol['price_usd'])
    # Live fallback
    try:
        import requests
        r = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{_SOL_MINT}',
            timeout=10)
        pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == 'solana']
        if pairs:
            best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
            return float(best.get('priceUsd', 0) or 0)
    except Exception as e:
        print(f'[WARN] SOL price error: {e}', file=sys.stderr)
    return 0.0


def get_token_price_usd(address: str) -> float:
    """Token price: screener cache first, then live DexScreener fallback."""
    cache = _load_screener_cache()
    entry = cache.get(address) or cache.get(address.lower())
    if entry and entry.get('price_usd', 0) > 0:
        return float(entry['price_usd'])
    # Live fallback
    try:
        import requests
        r = requests.get(
            f'https://api.dexscreener.com/latest/dex/tokens/{address}',
            headers={'Accept': 'application/json'}, timeout=10)
        pairs = [p for p in r.json().get('pairs', []) if p.get('chainId') == 'solana']
        if pairs:
            best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
            return float(best.get('priceUsd', 0) or 0)
    except Exception:
        pass
    return 0.0


def pnl_class(val: float) -> str:
    if val > 0:
        return 'color:#16a34a;font-weight:bold'
    if val < 0:
        return 'color:#dc2626;font-weight:bold'
    return 'color:#888'


# Inline style helpers — required for Gmail rendering
_S = {
    "body":   "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#222;max-width:900px;margin:0 auto;padding:20px",
    "h1":     "color:#1a1a2e;border-bottom:2px solid #9945ff;padding-bottom:8px",
    "h2":     "color:#333;margin-top:24px;font-size:16px",
    "table":  "border-collapse:collapse;width:100%;margin:8px 0",
    "th":     "background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px",
    "td":     "padding:5px 10px;border-bottom:1px solid #eee;font-size:13px",
    "td_alt": "padding:5px 10px;border-bottom:1px solid #eee;font-size:13px;background:#f8f8f8",
    "green":  "color:#16a34a;font-weight:bold",
    "red":    "color:#dc2626;font-weight:bold",
    "gray":   "color:#888",
    "footer": "margin-top:30px;font-size:11px;color:#999;border-top:1px solid #eee;padding-top:10px",
}


def write_wallet_snapshot(sol_balance: float, sol_price: float):
    """Append today's wallet balance to wallet_history.jsonl."""
    record = {
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'sol_balance': round(sol_balance, 6),
        'sol_price_usd': round(sol_price, 2),
        'usd_value': round(sol_balance * sol_price, 2),
    }
    path = DATA_DIR / 'wallet_history.jsonl'
    # Only write once per day
    existing = load_jsonl(path)
    if not any(e.get('ts') == record['ts'] for e in existing):
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')


def generate_html() -> str:
    now = datetime.now(timezone.utc)
    report_date = (now - timedelta(days=1)).strftime('%Y-%m-%d')

    trades_all = load_jsonl(DATA_DIR / 'trade_log.jsonl')
    runs_all = load_jsonl(DATA_DIR / 'run_log.jsonl')
    positions_path = DATA_DIR / 'positions.json'
    open_positions = []
    if positions_path.exists():
        try:
            data = json.loads(positions_path.read_text())
            open_positions = [p for p in data if p.get('status') in ('open', 'partial')]
        except Exception:
            pass

    # Filter to yesterday
    trades_yesterday = [t for t in trades_all if t.get('ts', '').startswith(report_date)]
    runs_yesterday = [r for r in runs_all if r.get('ts', '').startswith(report_date)]

    sol_balance = get_sol_balance()
    sol_price = get_sol_price_usd()
    sol_usd = sol_balance * sol_price

    # Write today's snapshot before rendering
    write_wallet_snapshot(sol_balance, sol_price)
    wallet_history = load_jsonl(DATA_DIR / 'wallet_history.jsonl')[-7:]  # last 7 days

    solscan_wallet = f'https://solscan.io/account/{WALLET}' if WALLET else '#'

    report_generated = now.strftime('%Y-%m-%d')
    parts = [f'<html><body style="{_S["body"]}">']
    parts.append(f'<h1 style="{_S["h1"]}">☀ Solana Trader Daily — {report_generated}</h1>')
    parts.append(f'<p style="{_S["gray"]};margin-top:-10px;font-size:12px">Activity for {report_date}</p>')

    # Wallet balance
    parts.append(f'<h2 style="{_S["h2"]}">Wallet</h2>')
    stat = "display:inline-block;background:#f8f8f8;border:1px solid #ddd;border-radius:6px;padding:10px 18px;margin:6px 8px 6px 0"
    lbl = "font-size:11px;color:#666"
    val = "font-size:20px;font-weight:bold"
    parts.append(f'<div style="{stat}"><div style="{lbl}">SOL Balance</div><div style="{val}">{sol_balance:.4f} SOL</div></div>'
                 f'<div style="{stat}"><div style="{lbl}">USD Value</div><div style="{val}">${sol_usd:,.2f}</div></div>'
                 f'<div style="{stat}"><div style="{lbl}">SOL Price</div><div style="{val}">${sol_price:,.2f}</div></div>')
    parts.append(f'<p><a href="{solscan_wallet}" target="_blank" style="color:#9945ff">🔗 View on Solscan</a></p>')

    # 7-day wallet balance trend
    if len(wallet_history) > 1:
        parts.append('<h2 style="color:#333;margin-top:24px;font-size:16px">7-Day Balance Trend</h2>')
        parts.append('<table style="border-collapse:collapse;width:100%;margin:8px 0"><tr><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Date</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">SOL Balance</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">SOL Price</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">USD Value</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Change</th></tr>')
        for i, entry in enumerate(wallet_history):
            prev = wallet_history[i-1] if i > 0 else None
            delta = entry['usd_value'] - prev['usd_value'] if prev else 0
            color = pnl_class(delta)
            delta_str = f'<span style="{color}">{delta:+.2f}</span>' if prev else '—'
            parts.append(f'<tr><td>{entry["ts"]}</td><td>{entry["sol_balance"]:.4f}</td>'
                         f'<td>${entry["sol_price_usd"]:,.2f}</td>'
                         f'<td><strong>${entry["usd_value"]:,.2f}</strong></td>'
                         f'<td>{delta_str}</td></tr>')
        parts.append('</table>')

    # Open positions
    parts.append(f'<h2 style="color:#333;margin-top:24px;font-size:16px">Open Positions ({len(open_positions)})</h2>')
    if open_positions:
        parts.append('''<table style="border-collapse:collapse;width:100%;margin:8px 0">
<tr><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Symbol</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Entry (SOL)</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Entry (USD)</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Size</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Mark P&L</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Status</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Score</th></tr>''')
        for pos in open_positions:
            symbol = pos.get('symbol', '?')
            address = pos.get('address', '')
            entry_sol = pos.get('entry_price_sol', 0)
            entry_usd = pos.get('entry_price_usd') or pos.get('entry_price', 0)
            position_usd = pos.get('position_usd', 0)
            score = pos.get('total_score', 0)
            status = pos.get('status', 'open')
            current_usd = get_token_price_usd(address) if address else 0
            if current_usd > 0 and entry_usd > 0:
                mark_pnl_pct = (current_usd - entry_usd) / entry_usd * 100
                mark_pnl_usd = (current_usd - entry_usd) / entry_usd * position_usd
                pnl_cell = f'<span style="{pnl_class(mark_pnl_pct)}">{mark_pnl_pct:+.1f}% (≈${mark_pnl_usd:+.2f})</span>'
            else:
                pnl_cell = '<span style="color:#888">N/A</span>'
            parts.append(f'''<tr>
<td><strong>{symbol}</strong></td>
<td>{entry_sol:.8f}</td><td>${entry_usd:.6f}</td>
<td>${position_usd:.0f}</td><td>{pnl_cell}</td>
<td>{status}</td><td>{score}</td>
</tr>''')
        parts.append('</table>')
    else:
        parts.append('<p style="color:#888">No open positions.</p>')

    # Trades
    buys = [t for t in trades_yesterday if t.get('action') == 'BUY']
    sells = [t for t in trades_yesterday if t.get('action') in ('SELL_TIER1', 'SELL_TIER2', 'SELL_TRAILING', 'STOP_LOSS', 'BREAKEVEN_STOP')]
    parts.append(f'<h2 style="color:#333;margin-top:24px;font-size:16px">Trades ({report_date}) — {len(buys)} buys, {len(sells)} sells</h2>')
    all_trades = sorted(trades_yesterday, key=lambda t: t.get('ts', ''))
    if all_trades:
        parts.append('''<table style="border-collapse:collapse;width:100%;margin:8px 0">
<tr><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Action</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Symbol</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Entry (SOL)</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Size</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Wallet P&L</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Score</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">TX</th></tr>''')
        for t in all_trades:
            action = t.get('action', 'BUY')
            symbol = t.get('symbol', '?')
            entry_sol = t.get('entry_price_sol', 0)
            position_usd = t.get('position_usd', 0)
            score = t.get('total_score', 0)
            tx = t.get('tx_hash') or t.get('tx') or ''
            tx_cell = (f'<a href="https://solscan.io/tx/{tx}" target="_blank" style="font-family:monospace;font-size:11px">'
                       f'{tx[:14]}...</a>') if tx else '<span style="color:#888">—</span>'
            is_buy = action == 'BUY'
            badge_cls = 'badge-buy' if is_buy else 'badge-sell'
            # P&L for sells
            if not is_buy:
                sol_spent = t.get('sol_spent', 0)
                sol_received = t.get('sol_received', 0)
                w_pnl_pct = t.get('wallet_pnl_pct')
                if w_pnl_pct is None and sol_spent > 0 and sol_received > 0:
                    w_pnl_pct = (sol_received - sol_spent) / sol_spent * 100
                if w_pnl_pct is not None:
                    sign = '+' if w_pnl_pct >= 0 else ''
                    sol_delta = sol_received - sol_spent if sol_spent and sol_received else 0
                    usd_delta = sol_delta * sol_price if sol_price else 0
                    color = pnl_class(w_pnl_pct)
                    pnl_cell = f'<span style="{color}">{sign}{w_pnl_pct:.1f}% ({sign}{sol_delta:.4f} SOL ≈ {sign}${usd_delta:.2f})</span>'
                else:
                    # Pre-fix records: use stored pnl_pct (ratio × 100 bug may exist — display as-is with note)
                    raw_pnl = t.get('pnl_pct', 0)
                    if abs(raw_pnl) > 100:
                        pnl_cell = f'<span style="color:#888">{raw_pnl:.1f}% (legacy — verify on-chain)</span>'
                    else:
                        color = pnl_class(raw_pnl)
                        pnl_cell = f'<span style="{color}">{raw_pnl:+.1f}%</span>'
            else:
                pnl_cell = '<span style="color:#888">open</span>'
            parts.append(f'''<tr>
<td><span style="{badge_cls}">{action}</span></td>
<td><strong>{symbol}</strong></td>
<td>{entry_sol:.8f}</td><td>${position_usd:.0f}</td>
<td>{pnl_cell}</td><td>{score}</td><td>{tx_cell}</td>
</tr>''')
        parts.append('</table>')
    else:
        parts.append("<p style='color:#888'>No trades yesterday.</p>")

    # Skipped due to cost
    skipped_all = load_jsonl(DATA_DIR / 'skipped_log.jsonl')
    skipped_yesterday = [s for s in skipped_all if s.get('ts', '').startswith(report_date)]
    if skipped_yesterday:
        parts.append(f'<h2 style="color:#333;margin-top:24px;font-size:16px">Skipped — Cost Too High ({report_date}) — {len(skipped_yesterday)} signals</h2>')
        parts.append('''<table style="border-collapse:collapse;width:100%;margin:8px 0">
<tr><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Symbol</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Score</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Size</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Slippage</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Fee</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Total Cost</th><th style="background:#f0f0f0;text-align:left;padding:6px 10px;font-size:12px">Threshold</th></tr>''')
        for s in sorted(skipped_yesterday, key=lambda x: x.get('score', 0), reverse=True):
            parts.append(f'''<tr>
<td><strong>{s.get("symbol","?")}</strong></td>
<td>{s.get("score",0)}</td>
<td>${s.get("position_usd",0):.0f}</td>
<td style="color:#dc2626;font-weight:bold">{s.get("est_slippage_pct",0):.2f}%</td>
<td style="color:#dc2626;font-weight:bold">{s.get("est_fee_pct",0):.2f}%</td>
<td style="color:#dc2626;font-weight:bold"><strong>{s.get("total_cost_pct",0):.2f}%</strong></td>
<td style="color:#888">{s.get("threshold",0):.1f}%</td>
</tr>''')
        parts.append('</table>')

    # Pipeline runs
    parts.append(f'<h2 style="color:#333;margin-top:24px;font-size:16px">Pipeline Runs ({report_date}) — {len(runs_yesterday)} total</h2>')
    if runs_yesterday:
        total_scanned = sum(r.get('tokens_scanned', 0) for r in runs_yesterday)
        total_executed = sum(r.get('trades_executed', 0) for r in runs_yesterday)
        errors = [e for r in runs_yesterday for e in r.get('errors', [])]
        parts.append(f'<p>Tokens scanned: <strong>{total_scanned}</strong> &nbsp;|&nbsp; '
                     f'Trades executed: <strong>{total_executed}</strong> &nbsp;|&nbsp; '
                     f'Errors: <strong style="{"color:#dc2626;font-weight:bold" if errors else "color:#16a34a;font-weight:bold"}">{len(errors)}</strong></p>')
        if errors:
            parts.append('<ul>' + ''.join(f'<li style="color:#dc2626;font-weight:bold">{e}</li>' for e in errors[:10]) + '</ul>')
    else:
        parts.append("<p style='color:#888'>No runs logged.</p>")

    parts.append(f'<div style="margin-top:30px;font-size:11px;color:#999;border-top:1px solid #eee;padding-top:10px">Generated {now.strftime("%Y-%m-%d %H:%M")} UTC &nbsp;|&nbsp; '
                 f'Wallet: {WALLET}</div>')
    parts.append('</body></html>')
    return '\n'.join(parts)


if __name__ == '__main__':
    print(generate_html())
