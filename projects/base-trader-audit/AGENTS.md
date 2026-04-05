# AGENTS.md — Base Trader Audit

## System
- Workspace: `/Users/jill/.hermes/workspace`
- Ralph Review Agent outputs to `review.md` in this project directory.

## What to Review

The Base Trader is a live trading system on Base mainnet:
1. Scans Nansen API for smart-money token activity
2. Scores signals — BUY triggers at score ≥ 175/400
3. Sizes position: min(20% wallet USD, 1% pool TVL USD)
4. Executes via Aerodrome DEX (execute.py) — WETH → token
5. Writes positions to data/positions.json
6. Sends Telegram alerts to group `-5264050975`
7. Monitors exits: stop loss (support × 0.92), target hit, or SM netflow negative

## Critical Spec Points to Verify Against Implementation

| Spec | Implementation must match |
|------|--------------------------|
| Nonce = pending block tag | `_get_nonce(w3, addr, use_pending=True)` |
| Wait for pending txs before trading | `_wait_for_pending_cleared()` called in buy_token + sell_token |
| Gas bump retry on replacement | 3 retries with 1.25× bump |
| Telegram → group `-5264050975` | grep for `374999219` → must be zero results |
| BUY alert amount = 20% wallet | `format_decision()` returns `position_usd = wallet × 0.20` |
| Status=failed on any error | execute.py catches all exceptions, never silently succeeds |
| Positions written after on-chain confirm | run_pipeline.py waits for receipt before writing |

## Files Under Review
- `scripts/execute.py` — core trading logic, nonce/gas handling
- `scripts/run_pipeline.py` — orchestration, Telegram routing
- `scripts/scanner.py` — Nansen API calls
- `scripts/signal_filter.py` — scoring
- `scripts/exit_monitor.py` — exit monitoring
- `scripts/verifier.py` — wallet reconciliation
- `scripts/decision.py` — position sizing
- `scripts/log_monitor.py` — log checking + alerting
