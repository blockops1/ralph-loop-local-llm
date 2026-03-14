#!/usr/bin/env python3
"""
Manual trade logger — records trades as JSONL entries.

Usage:
    echo '{"ts": "...", "symbol": "ETH", ...}' | python record_trade.py
    cat trade.json | python record_trade.py

Expected JSON keys: ts, symbol, address, amount_usd, entry_price, support, resistance, tx
Output: Appends one JSONL line to sandbox/data/trade_log.jsonl
"""

import json
import sys
from pathlib import Path


REQUIRED_KEYS = {"ts", "symbol", "address", "amount_usd", "entry_price", "support", "resistance", "tx"}
TRADE_LOG_PATH = Path(__file__).parent.parent / "data" / "trade_log.jsonl"


def main():
    # Read JSON from stdin
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            print("ERROR: No input received on stdin", file=sys.stderr)
            sys.exit(1)
        trade = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON — {e}", file=sys.stderr)
        sys.exit(1)

    # Validate required keys
    missing = REQUIRED_KEYS - set(trade.keys())
    if missing:
        print(f"ERROR: Missing required keys: {', '.join(sorted(missing))}", file=sys.stderr)
        sys.exit(1)

    # Ensure data directory exists
    TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Append as JSONL line
    try:
        with open(TRADE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, ensure_ascii=False) + "\n")
    except IOError as e:
        print(f"ERROR: Failed to write trade log — {e}", file=sys.stderr)
        sys.exit(1)

    # Success message
    print(f"Logged: {trade['symbol']}")


if __name__ == "__main__":
    main()
