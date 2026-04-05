#!/usr/bin/env python3
"""Generate daily Base Trader summary report as HTML."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import get_web3 with RPC fallback
sys.path.insert(0, str(Path(__file__).parent))
from execute import get_web3

DATA_DIR = Path(__file__).parent.parent / "data"


def load_jsonl(path):
    try:
        entries = []
        for line in Path(path).read_text().splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries
    except FileNotFoundError:
        return []


def get_credit_summary(data_dir: Path) -> dict:
    log_file = data_dir / "credit_log.jsonl"
    entries = load_jsonl(log_file)
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")
    today_used = yesterday_used = monthly_used = 0
    run_counts = []
    for e in entries:
        ts = e.get("ts", "")
        used = e.get("credits_used", 0)
        if ts.startswith(today_str):
            today_used += used
        if ts.startswith(yesterday_str):
            yesterday_used += used
        if ts.startswith(month_str):
            monthly_used += used
            run_counts.append(used)
    monthly_budget = 4000
    return {
        "today_used": today_used,
        "yesterday_used": yesterday_used,
        "monthly_used": monthly_used,
        "monthly_budget": monthly_budget,
        "budget_pct": (monthly_used / monthly_budget * 100) if monthly_budget else 0,
        "avg_per_run": (sum(run_counts) / len(run_counts)) if run_counts else 0,
    }


_NANSEN_CACHE_FILE = Path(__file__).parent.parent.parent / "shared" / "nansen_cache.json"
_GT_PRICE_CACHE_FILE = DATA_DIR / "gt_discovery_cache.json"


def _load_nansen_price_cache() -> dict:
    try:
        return json.load(open(_NANSEN_CACHE_FILE))
    except Exception:
        return {}


def get_current_price(address: str) -> float:
    """Return USD price from cache (Nansen screener cache, then GT discovery cache).
    Falls back to live DexScreener only if no cached data available.
    """
    addr = address.lower()

    # Primary: Nansen screener cache (shared across traders)
    nansen = _load_nansen_price_cache()
    entry = nansen.get(addr) or nansen.get(address)
    if entry and isinstance(entry, dict):
        price = float(entry.get("price_usd", 0) or 0)
        if price > 0:
            return price

    # Secondary: GT discovery cache
    try:
        gt = json.load(open(_GT_PRICE_CACHE_FILE))
        gt_entry = gt.get(addr) or gt.get(address)
        if gt_entry and isinstance(gt_entry, dict):
            price = float(gt_entry.get("price_usd", 0) or 0)
            if price > 0:
                return price
    except Exception:
        pass

    # Last resort: live DexScreener (only if no cache data)
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


# Alias for compatibility
get_current_price_gt = get_current_price


def _get_nansen_live_balance() -> int | None:
    """Fetch live Nansen credit balance from API. Returns int or None on error."""
    import requests
    key = os.environ.get("NANSEN_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.nansen.ai/api/v1/account",
            headers={"apikey": key},
            timeout=10,
        )
        if r.status_code == 200:
            val = r.json().get("credits_remaining", -1)
            return int(val) if val >= 0 else None
    except Exception:
        pass
    return None


def get_eth_balance() -> float:
    try:
        wallet = os.environ.get("BASE_WALLET_ADDRESS", "")
        if not wallet:
            return 0.0
        w3 = get_web3()
        balance_wei = w3.eth.get_balance(w3.to_checksum_address(wallet))
        return float(w3.from_wei(balance_wei, "ether"))
    except Exception:
        return 0.0


# ERC-20 balanceOf ABI (minimal)
_ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
]

# Known token addresses on Base chain (same wallet address works on both chains)
_BASE_KNOWN_TOKENS = {
    "ZEN":   "0xf43eb8de897fbc7f2502483b2bef7bb9ea179229",
    "VFY":   "0xa749de6c28262b7ffbc5de27dc845dd7ecd2b358",
    "ZRO":   "0x6985884c4392d348587b19cb9eaaf157f13271cd",
    "AERO":  "0x940181a94a35a4569e4529a3cdfb74e38fd98631",
    "AAVE":  "0x63706e401c06ac8513145b7687a14804d17f814b",
    "EIGEN": "0x2081ab0d9ec9e4303234ab26d86b20b3367946ee",
    "W":     "0xb0ffa8000886e57f86dd5264b9582b2ad87b2b91",
    "USDC":  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "WETH":  "0x4200000000000000000000000000000000000006",
}


def get_token_balances(wallet: str) -> list[dict]:
    """
    Fetch ERC-20 token balances for known tokens on Base chain.
    Returns list of {symbol, balance, address} for tokens with balance > 0.
    Uses 0.3s delay between calls to avoid mainnet.base.org rate limits.
    """
    import time
    try:
        from web3 import Web3
    except ImportError:
        return []

    results = []
    try:
        w3 = get_web3()
        wallet_cs = w3.to_checksum_address(wallet)
        for sym, addr in _BASE_KNOWN_TOKENS.items():
            time.sleep(0.3)
            try:
                c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=_ERC20_ABI)
                dec = c.functions.decimals().call()
                raw = c.functions.balanceOf(wallet_cs).call()
                bal = raw / 10 ** dec
                if bal > 0:
                    price = get_current_price(addr)
                    results.append({
                        "symbol": sym,
                        "balance": bal,
                        "address": addr,
                        "price_usd": price,
                        "value_usd": bal * price,
                    })
            except Exception:
                pass
    except Exception:
        pass
    return results


CSS = """
body { font-family: Arial, sans-serif; font-size: 14px; color: #222; background: #fff; margin: 0; padding: 16px; }
h1 { font-size: 20px; color: #1a1a2e; border-bottom: 2px solid #2563eb; padding-bottom: 6px; margin-bottom: 16px; }
h2 { font-size: 15px; color: #1e3a8a; margin-top: 20px; margin-bottom: 6px; border-left: 4px solid #2563eb; padding-left: 8px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 12px; }
th { background: #1e3a8a; color: #fff; padding: 6px 10px; text-align: left; font-size: 13px; }
td { padding: 5px 10px; border-bottom: 1px solid #e5e7eb; font-size: 13px; }
tr:nth-child(even) { background: #f8fafc; }
.green { color: #16a34a; font-weight: bold; }
.red { color: #dc2626; font-weight: bold; }
.gray { color: #6b7280; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: bold; }
.badge-buy { background: #dcfce7; color: #15803d; }
.badge-sell { background: #fee2e2; color: #b91c1c; }
.mono { font-family: monospace; font-size: 12px; }
.footer { margin-top: 24px; font-size: 11px; color: #9ca3af; border-top: 1px solid #e5e7eb; padding-top: 8px; }
"""


def pnl_class(val: float) -> str:
    if val > 0:
        return "green"
    if val < 0:
        return "red"
    return "gray"


def generate_html() -> str:
    now = datetime.now(timezone.utc)
    today = now
    yesterday = now - timedelta(days=1)
    # cutoff: midnight UTC yesterday — captures all events from yesterday
    cutoff = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    report_date = yesterday.strftime("%Y-%m-%d")   # activity window: yesterday
    report_generated = today.strftime("%Y-%m-%d")  # report generated: today

    # Load data
    all_trades = load_jsonl(DATA_DIR / "trade_log.jsonl")
    trades = [
        t for t in all_trades
        if datetime.fromisoformat(t["ts"].replace("Z", "+00:00")) > cutoff
        # Only show meaningful trade events — exclude PARTIAL_EXIT (null action/usd/score)
        and t.get("event") in ("BUY", "SELL", "EXIT", None)
        and t.get("action") in ("BUY", "SELL")
    ]
    signals = [
        s for s in load_jsonl(DATA_DIR / "signal_log.jsonl")
        if datetime.fromisoformat(s["ts"].replace("Z", "+00:00")) > cutoff
    ]

    try:
        raw_pos = json.load(open(DATA_DIR / "positions.json"))
        positions = list(raw_pos.values()) if isinstance(raw_pos, dict) else raw_pos
    except FileNotFoundError:
        positions = []

    open_positions = [p for p in positions if p.get("status") == "open"]

    eth_balance = get_eth_balance()

    # ------------------------------------------------------------------ HTML
    parts = [f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<h1>📊 Base Trader Daily — {report_generated}</h1>
<p class='gray' style='margin-top:-10px;font-size:12px'>Activity for {report_date}</p>
"""]

    # Wallet — ETH + known token balances
    wallet_addr = os.environ.get("BASE_WALLET_ADDRESS", "")
    basescan_wallet = f"https://basescan.org/address/{wallet_addr}" if wallet_addr else "#"
    eth_price = get_current_price("0x4200000000000000000000000000000000000006") or 2500
    eth_usd = eth_balance * eth_price
    token_balances = get_token_balances(wallet_addr) if wallet_addr else []
    total_wallet_usd = eth_usd + sum(t["value_usd"] for t in token_balances)

    parts.append("<h2>Wallet</h2>")
    parts.append(f'<p><a href="{basescan_wallet}" target="_blank">🔗 {wallet_addr[:10]}...</a> &nbsp; '
                 f'Total: <strong>~${total_wallet_usd:,.0f}</strong></p>')
    parts.append("""<table><tr><th>Asset</th><th>Balance</th><th>Price</th><th>Value</th></tr>""")
    parts.append(f'<tr><td><strong>ETH</strong></td><td>{eth_balance:.4f}</td>'
                 f'<td>${eth_price:,.2f}</td><td>${eth_usd:,.0f}</td></tr>')
    for tok in sorted(token_balances, key=lambda x: -x["value_usd"]):
        bal_str = f'{tok["balance"]:,.2f}' if tok["balance"] >= 0.01 else f'{tok["balance"]:.6f}'
        val_str = f'${tok["value_usd"]:,.2f}' if tok["value_usd"] >= 0.01 else '<$0.01'
        price_str = f'${tok["price_usd"]:.4f}' if tok["price_usd"] > 0 else '—'
        parts.append(f'<tr><td><strong>{tok["symbol"]}</strong></td><td>{bal_str}</td>'
                     f'<td>{price_str}</td><td>{val_str}</td></tr>')
    parts.append("</table>")

    # Open Positions
    parts.append(f"<h2>Open Positions ({len(open_positions)})</h2>")
    if open_positions:
        parts.append("""<table>
<tr><th>Symbol</th><th>Tokens</th><th>Cost</th><th>Entry</th><th>Current</th>
<th>Stop</th><th>Target</th><th>P&amp;L</th><th>Score</th><th>Opened</th></tr>""")
        for p in open_positions:
            symbol = p.get("symbol", "?")
            address = p.get("address", "")
            entry = p.get("entry_price", 0)
            stop = p.get("stop_loss", 0)
            target = p.get("target", 0)
            bal = p.get("token_balance", 0)
            cost = p.get("position_usd", 0)
            score = p.get("score", 0)
            opened = p.get("opened_at", "")[:10]

            current = get_current_price_gt(address)
            if current and entry:
                pnl_pct = (current - entry) / entry * 100
                pnl_usd = (current - entry) * bal
                pnl_cell = f'<span class="{pnl_class(pnl_usd)}">${pnl_usd:+.2f} ({pnl_pct:+.1f}%)</span>'
                current_cell = f"${current:.4f}"
            else:
                pnl_cell = '<span class="gray">N/A</span>'
                current_cell = '<span class="gray">unavail</span>'

            parts.append(f"""<tr>
<td><strong>{symbol}</strong></td>
<td>{bal:.2f}</td><td>${cost:.0f}</td>
<td>${entry:.4f}</td><td>{current_cell}</td>
<td>${stop:.4f}</td><td>${target:.4f}</td>
<td>{pnl_cell}</td><td>{score}</td><td>{opened}</td>
</tr>""")
        parts.append("</table>")
    else:
        parts.append("<p class='gray'>No open positions.</p>")

    # Trades
    parts.append(f"<h2>Trades ({report_date}) — {len(trades)} total</h2>")
    if trades:
        parts.append("""<table>
<tr><th>Action</th><th>Symbol</th><th>Price</th><th>USD</th><th>Score</th><th>TX</th></tr>""")
        for t in trades:
            symbol = t.get("symbol", "?")
            action = t.get("action", "BUY")
            price = t.get("entry_price", t.get("exit_price", 0))
            usd = t.get("position_usd", 0)
            score = t.get("score", 0)
            tx_hash = ""
            exec_data = t.get("exec") or {}
            if isinstance(exec_data, dict):
                tx_hash = exec_data.get("tx_hash", "")
            tx_cell = (f'<a href="https://basescan.org/tx/{tx_hash}" target="_blank" class="mono">'
                       f'{tx_hash[:14]}...</a>') if tx_hash else '<span class="gray">—</span>'
            badge_class = "badge-buy" if action == "BUY" else "badge-sell"
            parts.append(f"""<tr>
<td><span class="badge {badge_class}">{action}</span></td>
<td><strong>{symbol}</strong></td>
<td>${price:.4f}</td><td>${usd:.0f}</td><td>{score}</td>
<td>{tx_cell}</td>
</tr>""")
        parts.append("</table>")
    else:
        parts.append("<p class='gray'>No trades yesterday.</p>")

    # Signal scan
    passed = [s for s in signals if s.get("passes")]
    disqualified = [s for s in signals if s.get("disqualified")]
    below = [s for s in signals if not s.get("passes") and not s.get("disqualified")]

    parts.append(f"<h2>Signal Scan ({report_date})</h2>")
    parts.append(f"""<table>
<tr><th>Evaluated</th><th>Passed (≥175)</th><th>Disqualified</th><th>Below threshold</th></tr>
<tr><td>{len(signals)}</td><td>{len(passed)}</td><td>{len(disqualified)}</td><td>{len(below)}</td></tr>
</table>""")

    open_syms = {p.get("symbol") for p in open_positions}
    top_missed = sorted(
        [s for s in signals if s.get("passes") and s.get("symbol") not in open_syms],
        key=lambda x: x.get("score", 0), reverse=True
    )[:5]
    if top_missed:
        parts.append("<p><strong>Top candidates (not bought):</strong></p>")
        parts.append("""<table><tr><th>Symbol</th><th>Score</th><th>Reason skipped</th></tr>""")
        for s in top_missed:
            reason = s.get("action_taken", "")
            parts.append(f"<tr><td>{s['symbol']}</td><td>{s.get('score',0)}</td><td class='gray'>{reason}</td></tr>")
        parts.append("</table>")

    # Nansen credits — fetch live balance from API + local usage log
    creds = get_credit_summary(DATA_DIR)
    nansen_balance = _get_nansen_live_balance()
    budget_color = "red" if (nansen_balance is not None and nansen_balance < 500) else "green"
    parts.append("<h2>Nansen Credits</h2>")
    balance_cell = (f'<span class="{budget_color}"><strong>{nansen_balance:,} remaining</strong></span>'
                    if nansen_balance is not None else '<span class="gray">unavailable</span>')
    parts.append(f"""<table>
<tr><th>Live Balance</th><th>Used Today</th><th>Used Yesterday</th><th>Used This Month</th><th>Avg/run</th></tr>
<tr>
<td>{balance_cell}</td>
<td>{creds['today_used']}</td>
<td>{creds['yesterday_used']}</td>
<td>{creds['monthly_used']}</td>
<td>{creds['avg_per_run']:.1f}</td>
</tr></table>""")

    # Market Heat Index
    mhi_file = DATA_DIR / 'market_heat.json'
    if mhi_file.exists():
        try:
            mhi = json.loads(mhi_file.read_text())
            state = mhi.get('state', 'unknown').upper()
            score = mhi.get('score', 0)
            ts = mhi.get('timestamp', '')[:16].replace('T', ' ')
            state_color = {'COLD': '#3b82f6', 'WARM': '#f59e0b', 'HOT': '#ef4444'}.get(state, '#6b7280')
            state_emoji = {'COLD': '🧊', 'WARM': '🔥', 'HOT': '🚨'}.get(state, '📊')
            parts.append("<h2>Market Heat Index</h2>")
            parts.append(f"<p><strong style='color:{state_color}'>{state_emoji} {state}</strong> &nbsp; Score: <strong>{score}/100</strong> &nbsp; <span style='color:#9ca3af'>({ts} UTC)</span></p>")
            parts.append("<table><tr><th>Indicator</th><th>Score</th><th>Weight</th><th>Reason</th></tr>")
            for name, d in mhi.get('indicators', {}).items():
                parts.append(f"<tr><td>{name.replace('_',' ')}</td><td>{d['score']}</td><td>{d['weight']}%</td><td>{d['reason']}</td></tr>")
            parts.append("</table>")
        except Exception as e:
            parts.append(f"<p style='color:#9ca3af'>Market Heat Index unavailable: {e}</p>")
    else:
        parts.append("<h2>Market Heat Index</h2><p style='color:#9ca3af'>No data yet — runs hourly.</p>")

    parts.append(f"<div class='footer'>Generated {now.strftime('%Y-%m-%d %H:%M')} UTC</div>")
    parts.append("</body></html>")

    return "\n".join(parts)


if __name__ == "__main__":
    print(generate_html())
