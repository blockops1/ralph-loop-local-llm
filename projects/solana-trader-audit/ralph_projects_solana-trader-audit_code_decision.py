import json, os, time
from datetime import datetime, timezone
from pathlib import Path

BLACKLIST_FILE = Path(__file__).parent.parent / 'data' / 'blacklist.json'
COOLDOWN_FILE = Path(__file__).parent.parent / 'data' / 'failed_buy_cooldown.json'
TRADE_LOG = Path(__file__).parent.parent / 'data' / 'trade_log.jsonl'
COOLDOWN_SECONDS = 14400  # 4 hours

def load_blacklist() -> set:
    if not BLACKLIST_FILE.exists(): return set()
    try: return set(json.loads(BLACKLIST_FILE.read_text()))
    except: return set()

def is_on_cooldown(symbol: str) -> bool:
    if not COOLDOWN_FILE.exists(): return False
    try:
        cd = json.loads(COOLDOWN_FILE.read_text())
        ts = cd.get(symbol, 0)
        return time.time() - ts < COOLDOWN_SECONDS
    except: return False

def set_cooldown(symbol: str):
    cd = {}
    if COOLDOWN_FILE.exists():
        try: cd = json.loads(COOLDOWN_FILE.read_text())
        except: cd = {}
    cd[symbol] = time.time()
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_FILE.write_text(json.dumps(cd))

def format_decision(scored: dict, sol_price_usd: float, config: dict) -> dict:
    """Format a buy decision for a passing signal."""
    position_usd = config.get('position_size_usd', 50.0)
    amount_sol = position_usd / sol_price_usd if sol_price_usd > 0 else 0
    # Entry price in SOL (token priceNative from DexScreener or from token dict)
    entry_price_usd = scored['token'].get('price_usd', 0)
    entry_price_sol = entry_price_usd / sol_price_usd if sol_price_usd > 0 else 0
    stop_pct = config.get('stop_loss_pct', 0.20)
    stop_loss = entry_price_sol * (1 - stop_pct)
    return {
        'symbol': scored['symbol'],
        'action': 'BUY',
        'address': scored['address'],
        'entry_price_sol': entry_price_sol,
        'entry_price_usd': entry_price_usd,
        'position_usd': position_usd,
        'amount_sol': amount_sol,
        'stop_loss': stop_loss,
        'tier1_target': entry_price_sol * 1.50,   # +50%
        'tier2_target': entry_price_sol * 2.00,   # +100%
        'total_score': scored['total_score'],
        'pillars': scored['pillars'],
        'ts': datetime.now(timezone.utc).isoformat(),
        'status': 'open',
        'tier1_hit': False,
        'tier2_hit': False,
        'peak_price_sol': entry_price_sol,
    }

def log_trade(trade: dict):
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG, 'a') as f:
        f.write(json.dumps(trade) + '\n')
