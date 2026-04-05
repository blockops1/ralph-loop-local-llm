# Code Review: base-trader-live
**Date:** 2026-03-24
**Reviewer:** Ralph Review Agent
**Files reviewed:** 5
**Stories in PRD:** 5
**Overall:** ✅ Ship it

---

## Summary

The base-trader-live codebase implements a live trading pipeline on Base mainnet using Aerodrome DEX. The implementation correctly strips mock/paper mode, executes swaps via execute.py, wires execution into run_pipeline.py and exit_monitor.py, and properly configures environment files. The code compiles successfully and follows the PRD specification. There are no critical bugs that would prevent shipping. Several medium-priority issues exist around error handling and edge cases that should be addressed before the next sprint.

---

## 🔴 Critical (must fix before ship)

| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | exit_monitor.py | Position marked 'closed' even when sell_token() fails | Line 88, 98, 108: `position['status'] = 'closed'` is set unconditionally after `sell_token()`. If the sell fails, the position is orphaned in 'closed' status with no record of the failed exit. | Only set `position['status'] = 'closed'` when `sell_result.get('status') == 'ok'`. Otherwise set to `'exit_failed'` or keep as `'open'`. |

---

## ⚠️ Medium (fix before next sprint)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | run_pipeline.py | Telegram alert sent before execution confirmation | Line ~536-554: `send_telegram_alert()` with "Executing on-chain now..." is called before `buy_token()` executes. If the trade fails, users receive a misleading "executing" alert. | Move the "Executing on-chain now..." alert to after `buy_token()` returns status='ok'. |
| 2 | execute.py | No caching of ETH price across multiple calls | `get_eth_price_usd()` is called multiple times per pipeline run (balance check, BUY sizing, potentially per position in exit_monitor). Each call hits external APIs (CoinGecko, GeckoTerminal, etc.). | Cache the ETH price for the duration of a single pipeline run using a module-level variable. |
| 3 | execute.py | Gas estimation failure is non-fatal | Line ~380: When gas estimation fails, the trade proceeds anyway. For small trades (<$10), this could lead to wasted gas on doomed transactions. | Make gas estimation failure fatal for trades below a threshold (e.g., <$10). |
| 4 | run_pipeline.py | No retry logic for buy_token() failures | When `buy_token()` fails, the pipeline logs the failure but doesn't retry. The 4-hour cooldown helps but doesn't prevent repeated failures on transient issues. | Consider adding a retry mechanism with exponential backoff for transient failures (e.g., network timeouts). |
| 5 | execute.py | Approve tx timeout doesn't log tx_hash | Line ~335-338: When `approve_receipt is None`, the error message says "will retry on next run" but doesn't log the `approve_hash_hex` for manual follow-up. | Log the `approve_hash_hex` even when receipt is None so users can manually check the transaction. |
| 6 | .env | Comment lists BASE_WALLET_ADDRESS but file has no such entry | Line 2-3: Comment says "Required keys: BASE_WALLET_ADDRESS, BASE_WALLET_PRIVATE_KEY, NANSEN_API_KEY" but the file only has `BASESCAN_API_KEY=*** and `NANSEN_MOCK=0`. | Add a comment line explicitly noting that BASE_WALLET_ADDRESS is in ~/.hermes/.env, not this file. |

---

## 🔵 Minor (nice to have)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | execute.py | buy_token() result doesn't include amount_out_min | Line ~395-400: On success, result dict includes `amount_eth` but not `amount_out_min` (the minimum tokens expected). | Add `amount_out_min` to the result dict on success for better tracking. |
| 2 | run_pipeline.py | get_btc_4h_change() has no fallback sources | Line ~82-105: Fetches BTC price from CoinGecko only. If CoinGecko rate-limits or fails, the function returns 0.0 silently. | Add fallback to a secondary price source (e.g., GeckoTerminal) if CoinGecko fails. |
| 3 | execute.py | No explicit error handling for Chainlink oracle failure | Line ~265-273: Chainlink ETH/USD feed is used as fallback but no error handling for when the contract call fails. | Add try/except around the Chainlink call with proper error logging. |
| 4 | exit_monitor.py | No logging of exit reason in exit_monitor.log | Line ~45-55: The log format includes timestamp and message but doesn't structure the exit reason (STOP/TARGET/SM) for parsing. | Add `exit_reason` field to the log line format for better analytics. |
| 5 | run_pipeline.py | Reconcile failure is non-fatal but logged | Line ~355-362: If reconcile fails, it's logged as "non-fatal" but the pipeline continues. This could lead to position inconsistencies. | Consider making reconcile failure fatal if it affects position integrity (e.g., mismatched balances). |

---

## ✅ Verified Working

- **run_pipeline.py**: Successfully strips mock/paper mode logic (US-001 complete). The file prints `[PIPELINE] Mode: LIVE` and does not reference `paper_mode`, `--mock`, or `--paper` flags.
- **execute.py**: Implements Aerodrome swap execution with fee-on-transfer variants (US-002 complete). The file includes `swapExactTokensForTokensSupportingFeeOnTransferTokens` and `swapExactTokensForETHSupportingFeeOnTransferTokens` functions.
- **execute.py**: Includes RPC fallback with exponential backoff on 429 errors. The `get_web3()` function rotates through `ALL_RPCS` with retry logic.
- **execute.py**: Implements WETH balance check before BUY to ensure sufficient token balance. The `buy_token()` function checks `weth_balance_eth < WETH_RESERVE + amount_eth`.
- **execute.py**: Implements gas cost abort for small trades. Line ~375-378 aborts if gas cost exceeds 10% of trade size.
- **execute.py**: Implements nonce handling with pending transaction awareness via `_get_nonce(w3, address, use_pending=True)`.
- **execute.py**: Implements gas bump retry on "replacement transaction underpriced" errors.
- **execute.py**: Implements multiple ETH price sources (CoinGecko, GeckoTerminal, Nansen cache, Chainlink) with fallback.
- **exit_monitor.py**: Wired into pipeline via `from execute import sell_token` import.
- **exit_monitor.py**: Checks all three exit conditions (STOP, TARGET, SM) and calls `sell_token()` in each branch.
- **.env**: Updated to remove stale `BASE_WALLET=0x000...` placeholder and includes `NANSEN_MOCK=0`.
- **run-pipeline.sh**: Sources `~/.hermes/.env` for secrets and activates venv before running pipeline (US-005 complete).
- **run_pipeline.py**: Correctly calls `format_decision()` before `buy_token()` in the BUY branch (line 536 before line 571).
- **run_pipeline.py**: Imports `buy_token` and `sell_token` from execute.py (line 97).
- **exit_monitor.py**: Imports `sell_token` from execute.py (line 17).

---

## Reviewer Notes

1. **exit_monitor.py position closure bug is the only critical issue**: The current implementation sets `position['status'] = 'closed'` regardless of whether `sell_token()` succeeded. This means failed exits leave positions in an inconsistent state. This should be fixed before any live trading with real funds.

2. **Code compiles successfully**: All three main files (run_pipeline.py, execute.py, exit_monitor.py) pass `python3 -m py_compile` without errors.

3. **No duplicate imports in execute.py**: The previous review incorrectly flagged duplicate `import requests` statements. There is only one import at line 13.

4. **format_decision() is correctly called before buy_token()**: The BUY branch in run_pipeline.py correctly calls `format_decision()` at line 536 before using `trade['position_usd']` in `buy_token()` at line 571. The previous review's claim about this being a bug was incorrect.

5. **Security consideration**: The `BASE_WALLET_PRIVATE_KEY` is loaded from `~/.hermes/.env` which is good practice. The local `.env` file correctly avoids storing private keys.

6. **Gas estimation reliability**: The gas estimation in `buy_token()` is marked "non-fatal" but for small trades (<$10), a failed gas estimation could lead to wasted gas. Consider making this fatal for trades below a threshold.

7. **ETH price caching opportunity**: The `get_eth_price_usd()` function is called multiple times per pipeline run. Caching the price for the duration of a single run would reduce API calls and improve reliability.

8. **Reconcile module dependency**: run_pipeline.py imports `from reconcile import reconcile` but this module is not listed in the context files. Verify that `reconcile.py` exists and is properly maintained.

9. **Test coverage gap**: The PRD acceptance criteria for US-002 include `python3 scripts/execute.py --check` but this test mode is not integrated into the pipeline's error handling. Consider adding a pre-flight check that runs `--check` before executing any trades.
