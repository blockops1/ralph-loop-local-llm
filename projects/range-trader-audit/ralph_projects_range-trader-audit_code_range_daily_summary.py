#!/usr/bin/env python3
"""Generate daily Range Trader summary report as HTML.

Uses inline styles throughout — required for reliable Gmail rendering.
"""

import io
import json
import os
import sys
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
SCRIPT_DIR = Path(__file__).parent

USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
]

# Known tokens to show in wallet section
KNOWN_TOKENS = {
    "ZEN":   "0xf43eb8de897fbc7f2502483b2bef7bb9ea179229",
    "VFY":   "0xa749de6c28262b7ffbc5de27dc845dd7ecd2b358",
    "ZRO":   "0x6985884c4392d348587b19cb9eaaf157f13271cd",
    "AAVE":  "0x63706e401c06ac8513145b7687a14804d17f814b",
    "W":     "0xb0ffa8000886e57f86dd5264b9582b2ad87b2b91",
    "USDC":  USDC_ADDRESS,
    "WETH":  WETH_ADDRESS,
}

# ---------------------------------------------------------------------------
# Inline style helpers (Gmail-safe)
# ---------------------------------------------------------------------------
_S = {
    "body":   "font-family:Arial,sans-serif;font-size:14px;color:#222;background:#fff;margin:0;padding:16px",
    "h1":     "font-size:20px;color:#1a1a2e;border-bottom:2px solid #7c3aed;padding-bottom:6px;margin-bottom:16px",
    "h2":     "font-size:15px;color:#4c1d95;margin-top:20px;margin-bottom:6px;border-left:4px solid #7c3aed;padding-left:8px",
    "table":  "border-collapse:collapse;width:100%;margin-bottom:12px",
    "th":     "background:#4c1d95;color:#fff;padding:6px 10px;text-align:left;font-size:13px",
    "td":     "padding:5px 10px;border-bottom:1px solid #e5e7eb;font-size:13px",
    "td_alt": "padding:5px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;background:#f8fafc",
    "green":  "color:#16a34a;font-weight:bold",
    "red":    "color:#dc2626;font-weight:bold",
    "orange": "color:#d97706;font-weight:bold",
    "gray":   "color:#6b7280",
    "badge":  "display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:bold",
    "footer": "margin-top:24px;font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:8px",
    "alert":  "background:#fef2f2;border:1px solid #fca5a5;border-radius:4px;padding:8px 12px;margin:8px 0;color:#991b1b",
}

def _td(content, i=0, extra=""):
    style = (_S["td_alt"] if i % 2 else _S["td"]) + (f";{extra}" if extra else "")
    return f'<td style="{style}">{content}</td>'

def _th(content):
    return f'<th style="{_S["th"]}">{content}</th>'

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_jsonl(path):
    try:
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


_GT_PRICE_CACHE_FILE = DATA_DIR / "gt_price_cache.json"


def _load_gt_price_cache() -> dict:
    try:
        return json.load(open(_GT_PRICE_CACHE_FILE))
    except Exception:
        return {}


def get_current_price_usd(address: str) -> float:
    """Return USD price from GT price cache first, then live DexScreener fallback."""
    addr = address.lower()

    # Primary: GT price cache (written by scanner every pipeline run)
    cache = _load_gt_price_cache()
    # Cache key format: "base:<addr_lower>"
    entry = cache.get(f"base:{addr}") or cache.get(addr)
    if entry and isinstance(entry, dict):
        price = float(entry.get("data", {}).get("price_usd", 0) or 0)
        if price > 0:
            return price

    # Fallback: live DexScreener
    import requests
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addr}",
            headers={"Accept": "application/json"}, timeout=10)
        if r.status_code == 200:
            pairs = [p for p in r.json().get("pairs", []) if p.get("chainId") == "base"]
            if pairs:
                best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                price = float(best.get("priceUsd", 0) or 0)
                if price > 0:
                    return price
    except Exception:
        pass
    return 0.0


def get_wallet_balances(wallet: str) -> dict:
    """Return {eth, usdc, weth, tokens: [{symbol, balance, value_usd}]}."""
    import time
    result = {"eth": 0.0, "usdc": 0.0, "weth": 0.0, "total_usd": 0.0, "tokens": []}
    if not wallet:
        return result
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
        wallet_cs = Web3.to_checksum_address(wallet)
        eth = float(w3.from_wei(w3.eth.get_balance(wallet_cs), "ether"))
        result["eth"] = eth

        eth_price = get_current_price_usd(WETH_ADDRESS) or 2100
        result["total_usd"] = eth * eth_price

        for sym, addr in KNOWN_TOKENS.items():
            time.sleep(0.3)
            try:
                c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
                dec = c.functions.decimals().call()
                bal = c.functions.balanceOf(wallet_cs).call() / 10 ** dec
                if bal > 0.0001:
                    price = get_current_price_usd(addr)
                    val = bal * price
                    result["total_usd"] += val
                    if sym == "USDC":
                        result["usdc"] = bal
                    elif sym == "WETH":
                        result["weth"] = bal
                    result["tokens"].append({"symbol": sym, "balance": bal, "price": price, "value": val})
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] wallet error: {e}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html() -> str:
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    cutoff = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    report_date = yesterday.strftime("%Y-%m-%d")
    report_generated = now.strftime("%Y-%m-%d")

    # Load data
    all_trades = load_jsonl(DATA_DIR / "range_trade_log.jsonl")
    trades_today = [
        t for t in all_trades
        if datetime.fromisoformat(t["ts"].replace("Z", "+00:00")) > cutoff
    ]
    run_log = load_jsonl(DATA_DIR / "range_run_log.jsonl")
    runs_today = [
        r for r in run_log
        if datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) > cutoff
    ]

    try:
        raw_pos = json.load(open(DATA_DIR / "range_positions.json"))
        positions = list(raw_pos.values()) if isinstance(raw_pos, dict) else raw_pos
    except FileNotFoundError:
        positions = []

    # Positions without a "status" field are implicitly open (range_positions.json doesn't use status)
    open_positions = [p for p in positions if p.get("status", "open") == "open"]
    failed_trades = [t for t in trades_today if t.get("status") == "failed"]
    success_trades = [t for t in trades_today if t.get("status") != "failed"]

    wallet_addr = os.environ.get("METAMASK_WALLET_ADDRESS", "")
    balances = get_wallet_balances(wallet_addr)

    parts = [f'<html><body style="{_S["body"]}">']
    parts.append(f'<h1 style="{_S["h1"]}">📊 Range Trader Daily — {report_generated}</h1>')
    parts.append(f'<p style="{_S["gray"]};margin-top:-10px;font-size:12px">Activity for {report_date}</p>')

    # Errors alert — only show errors from TODAY's runs (not yesterday's historical errors)
    today_str = now.strftime("%Y-%m-%d")
    today_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    runs_today_only = [
        r for r in run_log
        if datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) > today_cutoff
    ]
    run_errors = []
    for r in runs_today_only:
        run_errors.extend(r.get("errors", []))
    unique_errors = list(dict.fromkeys(run_errors))
    if unique_errors:
        parts.append(f'<div style="{_S["alert"]}"><strong>⚠️ Errors today ({today_str}):</strong><ul style="margin:4px 0;padding-left:20px">')
        for e in unique_errors:
            parts.append(f"<li>{e}</li>")
        parts.append("</ul></div>")

    # Wallet
    basescan = f"https://basescan.org/address/{wallet_addr}" if wallet_addr else "#"
    parts.append(f'<h2 style="{_S["h2"]}">Wallet</h2>')
    parts.append(f'<p><a href="{basescan}" target="_blank">🔗 {wallet_addr[:10]}...</a> &nbsp; '
                 f'Total: <strong>~${balances["total_usd"]:,.0f}</strong></p>')
    parts.append(f'<table style="{_S["table"]}"><tr>')
    for h in ["Asset", "Balance", "Price (USD)", "Value (USD)"]:
        parts.append(_th(h))
    parts.append("</tr>")
    eth_price = get_current_price_usd(WETH_ADDRESS) or 2100
    eth_bal = balances["eth"]
    eth_val = eth_bal * eth_price
    parts.append(f'<tr>{_td("<strong>ETH</strong>")}{_td(f"{eth_bal:.4f}")}'
                 f'{_td(f"${eth_price:,.2f}")}{_td(f"${eth_val:,.2f}")}</tr>')
    for i, tok in enumerate(sorted(balances["tokens"], key=lambda x: -x["value"]), 1):
        bal_str = f'{tok["balance"]:,.4f}' if tok["balance"] < 1000 else f'{tok["balance"]:,.2f}'
        sym = tok["symbol"]
        price = tok["price"]
        val = tok["value"]
        parts.append(f'<tr>{_td(f"<strong>{sym}</strong>",i)}'
                     f'{_td(bal_str,i)}{_td(f"${price:.4f}",i)}'
                     f'{_td(f"${val:,.2f}",i)}</tr>')
    parts.append("</table>")

    # Token Ranges — all values in USD
    parts.append(f'<h2 style="{_S["h2"]}">Token Ranges (USD)</h2>')
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from range_scanner import scan_all
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            scan_results = scan_all()
        print(_buf.getvalue(), end='', file=sys.stderr)

        from range_signal_filter import SCORE_THRESHOLD, ENTRY_BUFFER_PCT, MIN_RANGE_WIDTH_PCT

        parts.append(f'<table style="{_S["table"]}"><tr>')
        open_symbols = {p.get("symbol", "").upper() for p in open_positions}
        for h in ["Symbol", "Support $", "Resist $", "Width", "Price $", "% thru", "Score", "Status"]:
            parts.append(_th(h))
        parts.append("</tr>")

        for i, r in enumerate(scan_results):
            sym = r.get("symbol", "")
            sr = r.get("sr") or {}
            sup = sr.get("support")
            res = sr.get("resistance")
            price_usd = r.get("price_usd", 0)  # Use USD price, not ETH
            score = r.get("range_score", 0)
            error = r.get("error")
            near_sup = sr.get("near_support", False)
            near_res = sr.get("near_resistance", False)

            if error or not sup or not res:
                reason = error or "No S/R data"
                gray = _S["gray"]
                no_data_cell = f'<span style="{gray}">{reason}</span>'
                row = f'<tr>{_td(f"<strong>{sym}</strong>",i)}' + ''.join(_td("—", i) for _ in range(6)) + f'{_td(no_data_cell,i)}</tr>'
                parts.append(row)
                continue

            width_pct = (res - sup) / sup * 100 if sup > 0 else 0
            width_color = _S["green"] if width_pct >= MIN_RANGE_WIDTH_PCT else _S["red"]
            width_str = f'<span style="{width_color}">{width_pct:.0f}%</span>'

            if price_usd and sup and res and (res - sup) > 0:
                pct_thru = (price_usd - sup) / (res - sup) * 100
                pct_str = f"{pct_thru:.0f}%"
            else:
                pct_thru = None
                pct_str = "—"

            # Status
            if sym.upper() in open_symbols:
                status = f'<span style="{_S["gray"]}">📂 Already Open</span>'
            elif near_sup:
                status = f'<span style="{_S["green"]}">🟢 At Support</span>'
            elif near_res:
                status = f'<span style="{_S["red"]}">🔴 At Resist</span>'
            elif pct_thru is not None and pct_thru < -15:
                status = f'<span style="{_S["red"]}">Broken Range</span>'
            elif pct_thru is not None and pct_thru < 0:
                status = f'<span style="{_S["orange"]}">Below Support</span>'
            elif pct_thru is not None and pct_thru > 100:
                status = f'<span style="{_S["orange"]}">Above Resist</span>'
            else:
                status = f'<span style="{_S["gray"]}">Mid Range</span>'

            score_style = _S["green"] if score >= SCORE_THRESHOLD else _S["gray"]
            parts.append(
                f'<tr>{_td(f"<strong>{sym}</strong>",i)}'
                f'{_td(f"${sup:.4f}",i)}{_td(f"${res:.4f}",i)}'
                f'{_td(width_str,i)}{_td(f"${price_usd:.4f}",i)}'
                f'{_td(pct_str,i)}{_td(f"<span style=\"{score_style}\">{score}</span>",i)}'
                f'{_td(status,i)}</tr>'
            )
        parts.append("</table>")
    except Exception as e:
        parts.append(f'<p style="{_S["gray"]}">Range scan unavailable: {e}</p>')

    # Open Positions
    parts.append(f'<h2 style="{_S["h2"]}">Open Positions ({len(open_positions)})</h2>')
    if open_positions:
        parts.append(f'<table style="{_S["table"]}"><tr>')
        for h in ["Symbol", "Tokens", "Cost (USDC)", "Entry $", "Current $", "Stop $", "Target $", "P&L", "Score", "Opened"]:
            parts.append(_th(h))
        parts.append("</tr>")
        for i, p in enumerate(open_positions):
            sym = p.get("symbol", "?")
            addr = p.get("address", "")
            entry = p.get("entry_price", 0)
            stop = p.get("stop_loss", 0)
            target = p.get("exit_price", p.get("target", 0))
            bal = p.get("tokens_held", p.get("token_balance", 0))
            cost = p.get("entry_usdc", p.get("position_usd", 0))
            score = p.get("score", 0)
            opened = p.get("entry_ts", p.get("opened_at", ""))[:10]
            current = get_current_price_usd(addr) if addr else 0
            if current and entry:
                pnl_pct = (current - entry) / entry * 100
                pnl_usd = cost * pnl_pct / 100 if cost else 0
                pnl_style = _S["green"] if pnl_usd > 0 else _S["red"]
                pnl_cell = f'<span style="{pnl_style}">${pnl_usd:+.2f} ({pnl_pct:+.1f}%)</span>'
                cur_cell = f"${current:.4f}"
            else:
                pnl_cell = f'<span style="{_S["gray"]}">N/A</span>'
                cur_cell = f'<span style="{_S["gray"]}">—</span>'
            parts.append(
                f'<tr>{_td(f"<strong>{sym}</strong>",i)}'
                f'{_td(f"{bal:.2f}",i)}{_td(f"${cost:.2f}",i)}'
                f'{_td(f"${entry:.4f}",i)}{_td(cur_cell,i)}'
                f'{_td(f"${stop:.4f}",i)}{_td(f"${target:.4f}",i)}'
                f'{_td(pnl_cell,i)}{_td(str(score),i)}{_td(opened,i)}</tr>'
            )
        parts.append("</table>")
    else:
        parts.append(f'<p style="{_S["gray"]}">No open positions.</p>')

    # Trades
    parts.append(f'<h2 style="{_S["h2"]}">Trades ({report_date}) — {len(success_trades)} executed, {len(failed_trades)} failed</h2>')
    if trades_today:
        parts.append(f'<table style="{_S["table"]}"><tr>')
        for h in ["Action", "Symbol", "Price $", "USDC", "Score", "Status", "TX"]:
            parts.append(_th(h))
        parts.append("</tr>")
        for i, t in enumerate(trades_today):
            sym = t.get("symbol", "?")
            action = t.get("action", "BUY")
            price = t.get("price_usd", t.get("entry_price", t.get("exit_price_actual", 0)))
            usdc = t.get("entry_usdc", t.get("position_usd", 0))
            score = t.get("score", t.get("range_score", 0))
            status = t.get("status", "ok")
            tx = t.get("tx_hash", "") or ""
            tx_cell = (f'<a href="https://basescan.org/tx/{tx}" target="_blank" '
                       f'style="font-family:monospace;font-size:11px">{tx[:12]}…</a>') if tx else "—"
            if status == "failed":
                badge_bg = "#fef3c7"; badge_color = "#92400e"
                status_style = _S["red"]
            elif action == "BUY":
                badge_bg = "#dcfce7"; badge_color = "#15803d"
                status_style = _S["green"]
            else:
                badge_bg = "#fee2e2"; badge_color = "#b91c1c"
                status_style = _S["green"]
            badge = (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
                     f'font-size:12px;font-weight:bold;background:{badge_bg};color:{badge_color}">'
                     f'{action}</span>')
            parts.append(
                f'<tr>{_td(badge,i)}{_td(f"<strong>{sym}</strong>",i)}'
                f'{_td(f"${price:.4f}",i)}{_td(f"${usdc:.2f}" if usdc else "—",i)}'
                f'{_td(str(score),i)}{_td(f"<span style=\"{status_style}\">{status}</span>",i)}'
                f'{_td(tx_cell,i)}</tr>'
            )
        parts.append("</table>")
    else:
        parts.append(f'<p style="{_S["gray"]}">No trade attempts yesterday.</p>')

    # Pipeline summary
    parts.append(f'<h2 style="{_S["h2"]}">Pipeline ({report_date}) — {len(runs_today)} runs</h2>')
    if runs_today:
        total_scanned = sum(r.get("tokens_scanned", 0) for r in runs_today)
        total_executed = sum(r.get("trades_executed", 0) for r in runs_today)
        total_errors = sum(len(r.get("errors", [])) for r in runs_today)
        err_style = _S["red"] if total_errors else _S["green"]
        parts.append(f'<p>Tokens scanned: <strong>{total_scanned}</strong> &nbsp;|&nbsp; '
                     f'Executed: <strong>{total_executed}</strong> &nbsp;|&nbsp; '
                     f'Errors: <strong style="{err_style}">{total_errors}</strong></p>')
    else:
        parts.append(f'<p style="{_S["gray"]}">No runs logged.</p>')

    # Market Heat Index (shared)
    mhi_file = Path(__file__).parent.parent.parent / "base-trader" / "data" / "market_heat.json"
    if mhi_file.exists():
        try:
            mhi = json.loads(mhi_file.read_text())
            state = mhi.get("state", "unknown").upper()
            score = mhi.get("score", 0)
            ts = mhi.get("timestamp", "")[:16].replace("T", " ")
            state_color = {"COLD": "#3b82f6", "WARM": "#f59e0b", "HOT": "#ef4444"}.get(state, "#6b7280")
            emoji = {"COLD": "🧊", "WARM": "🔥", "HOT": "🚨"}.get(state, "📊")
            parts.append(f'<h2 style="{_S["h2"]}">Market Heat Index</h2>')
            parts.append(f'<p><strong style="color:{state_color}">{emoji} {state}</strong> &nbsp; '
                         f'Score: <strong>{score}/100</strong> &nbsp; '
                         f'<span style="{_S["gray"]}">({ts} UTC)</span></p>')
            parts.append(f'<table style="{_S["table"]}"><tr>')
            for h in ["Indicator", "Score", "Weight", "Reason"]:
                parts.append(_th(h))
            parts.append("</tr>")
            for i, (name, d) in enumerate(mhi.get("indicators", {}).items()):
                ind_name = name.replace("_", " ")
                ind_score = str(d["score"])
                ind_weight = f'{d["weight"]}%'
                ind_reason = d["reason"]
                parts.append(f'<tr>{_td(ind_name,i)}{_td(ind_score,i)}{_td(ind_weight,i)}{_td(ind_reason,i)}</tr>')
            parts.append("</table>")
        except Exception as e:
            parts.append(f'<p style="{_S["gray"]}">Market Heat Index unavailable: {e}</p>')

    parts.append(f'<div style="{_S["footer"]}">Generated {now.strftime("%Y-%m-%d %H:%M")} UTC &nbsp;|&nbsp; Wallet: {wallet_addr}</div>')
    parts.append("</body></html>")
    return "\n".join(parts)


if __name__ == "__main__":
    print(generate_html())
