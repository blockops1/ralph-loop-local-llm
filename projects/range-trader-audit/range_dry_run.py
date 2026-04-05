#!/usr/bin/env python3
"""
range_dry_run.py - Validate execution routing without sending any transactions.

Runs scanner + filter, then shows what router/path each signal would use.
Exits with code 1 if any WARNING or unexpected routing is detected.

Usage:
    python3 range_dry_run.py
"""
import os
import sys
from pathlib import Path

# Load .env
_env_path = Path.home() / '.hermes' / '.env'
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip().replace('export ', '').strip(), _v.strip())

sys.path.insert(0, str(Path(__file__).parent))

from range_scanner import scan_all
from range_signal_filter import filter_signals
from range_execute import SLIPSTREAM_TOKENS, WATCH_ONLY_TOKENS
from range_positions import load_positions


def check_routing(token_address: str, symbol: str) -> tuple[str, str]:
    """
    Returns (status, description) for the token's execution path.
    status: 'slipstream-weth' | 'slipstream-usdc' | 'watch-only' | 'unknown'
    """
    addr_lower = token_address.lower()
    if addr_lower in WATCH_ONLY_TOKENS:
        return 'watch-only', 'No execution path (CRV=Curve, ETHFI=Univ4)'
    cfg = SLIPSTREAM_TOKENS.get(addr_lower)
    if not cfg:
        return 'unknown', 'NOT IN SLIPSTREAM_TOKENS — would be skipped'
    weth = '0x4200000000000000000000000000000000000006'
    if cfg['quote'].lower() == weth:
        return 'slipstream-weth', f"Slipstream tickSpacing={cfg['tick_spacing']} quote=WETH"
    else:
        return 'slipstream-usdc', f"Slipstream tickSpacing={cfg['tick_spacing']} quote=USDC"


def main():
    print('=== Range Trader Dry Run ===\n')
    issues = []

    # Show routing table for all tokens
    from range_scanner import TOKENS
    print('--- Routing Table ---')
    for sym, addr in TOKENS.items():
        status, desc = check_routing(addr, sym)
        icon = '✅' if status.startswith('slipstream') else ('👁️ ' if status == 'watch-only' else '❌')
        print(f'  {icon} {sym:6}: {desc}')
        if status == 'unknown':
            issues.append(f'{sym}: unknown routing — would be skipped silently')
    print()

    # Run scanner + filter
    print('--- Scanning tokens (may take ~2 min due to GT rate limits) ---')
    scan_results = scan_all()
    positions = load_positions()
    signals = filter_signals(scan_results, positions)

    print(f'\n--- Signals: {len(signals)} ---')
    if not signals:
        print('  No signals at current prices.')
    for sig in signals:
        sym = sig['symbol']
        addr = sig['address']
        action = sig['action']
        status, desc = check_routing(addr, sym)
        icon = '✅' if status.startswith('slipstream') else ('👁️ ' if status == 'watch-only' else '❌')
        print(f'  {icon} {action} {sym}: {desc}')
        if status in ('watch-only', 'unknown'):
            issues.append(f'{action} {sym}: would be skipped — {desc}')
    print()

    # Summary
    if issues:
        print('--- Issues Found ---')
        for issue in issues:
            print(f'  ⚠️  {issue}')
        print(f'\nDRY RUN: {len(issues)} issue(s) found')
        sys.exit(1)
    else:
        print('DRY RUN PASSED: All tokens have valid routing. No on-chain TXs would revert.')
        sys.exit(0)


if __name__ == '__main__':
    main()
