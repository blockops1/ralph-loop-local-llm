# Code Review: range-trader-ops
**Date:** 2026-03-27
**Reviewer:** Ralph Review Agent
**Files reviewed:** 5
**Stories in PRD:** 4
**Overall:** 🔴 Do not ship

---

## Summary
The range-trader-ops project is intended to harden the range-trader operational infrastructure by fixing the verifier for WETH-quoted positions, creating a log monitor with Telegram alerts, adding a launchd plist for scheduling, and integrating range-trader into the daily email report. Currently, **none of the 4 stories have been implemented**. The existing codebase contains the base range-trader components (scanner, verifier, positions, execute) but lacks all the operational hardening specified in the PRD. This is a significant gap — the range-trader will run without proper monitoring, alerting, or reporting integration.

---

## 🔴 Critical (must fix before ship)

| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | businesses/horizen-zkverify/range-trader/scripts/range_verifier.py | Telegram alert prefix uses `[VERIFIER]` instead of `[RANGE]` as specified in US-001 | Alerts from range-trader verifier are indistinguishable from base-trader alerts in Telegram, causing operational confusion | Change `send_telegram_alert()` prefix from `[VERIFIER]` to `[RANGE]` |
| 2 | businesses/horizen-zkverify/range-trader/scripts/range_verifier.py | No WETH balance check — verifier only checks token balances but range-trader trades against WETH (not USDC) on Aerodrome Slipstream | Positions may show as valid while wallet has insufficient WETH for gas or to cover position value; silent failure mode | Add `get_weth_balance()` helper and verify total WETH + ETH >= 0.005 ETH for gas floor |
| 3 | businesses/horizen-zkverify/range-trader/scripts/range_log_monitor.py | File does not exist — US-002 not implemented | No monitoring for pipeline heartbeat, error scans, verifier alarms, or WETH gas floor. Failed runs go undetected. | Create range_log_monitor.py modelled on base-trader's log_monitor.py |
| 4 | /Users/jill/Library/LaunchAgents/com.crestview.range-log-monitor.plist | Launchd plist does not exist — US-003 not implemented | range_log_monitor.py (even if created) will never run automatically; monitoring is manual only | Create plist with StartInterval=7200 and load via launchctl |
| 5 | businesses/horizen-zkverify/base-trader/scripts/daily_summary.py | No RANGE TRADER section — US-004 not implemented | Daily email report shows no range-trader positions, P&L, or wallet state. Operators blind to range-trader status. | Add `get_range_summary()` function and integrate into `generate_report()` |

---

## ⚠️ Medium (fix before next sprint)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | businesses/horizen-zkverify/range-trader/scripts/range_verifier.py | `verify_position()` catches specific errors but `main()` does not wrap the verification loop in try/except | A single RPC failure or JSON parse error crashes the entire verifier run, leaving partial results | Wrap the position loop in try/except with per-position error isolation |
| 2 | businesses/horizen-zkverify/range-trader/scripts/range_scanner.py | `get_price()` uses disk cache shared with verifier but no cache invalidation on write | Stale price data could persist if cache file is corrupted or partially written | Add file locking or atomic writes for GT_PRICE_CACHE_FILE |

---

## 🔵 Minor (nice to have)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | businesses/horizen-zkverify/range-trader/scripts/range_verifier.py | `--dry-run` flag prints `[DRY-RUN]` but does not log to JSONL | Cannot audit what would have been verified in dry-run mode | Log dry-run results to JSONL with `status='dry_run'` |
| 2 | businesses/horizen-zkverify/range-trader/scripts/range_positions.py | `load_positions()` returns empty list on JSON decode error but only prints warning to stderr | Silent degradation — caller may proceed with stale/missing position data | Consider raising exception or returning (list, error) tuple |

---

## ✅ Verified Working

- **range_scanner.py** correctly implements GeckoTerminal + DexScreener price fetching with disk caching (5 min TTL) shared across processes
- **range_positions.py** provides basic CRUD for position storage with proper file path handling
- **range_verifier.py** correctly validates on-chain token balances against logged positions with 25% discrepancy threshold
- **range_verifier.py** handles empty positions list gracefully (returns success with note)
- **daily_summary.py** (base-trader) has robust HTML report generation with wallet balances, trades, signals, and Nansen credit tracking

---

## Reviewer Notes

1. **Scope gap**: This PRD describes operational hardening for a running system, but none of the 4 stories are implemented. The range-trader core exists but lacks monitoring, alerting, and reporting infrastructure.

2. **WETH vs USDC confusion**: The PRD correctly identifies that range-trader trades against WETH on Aerodrome Slipstream, but the verifier was written assuming USDC-quoted positions. This is a fundamental mismatch that could cause silent failures.

3. **Test coverage**: No unit tests exist for any of the range-trader scripts. The acceptance criteria in the PRD are basic syntax/run checks, not functional tests.

4. **Dependency on base-trader**: The range-trader shares `credit_log.jsonl` with base-trader (per PRD), but this coupling is not documented in the code. A failure in base-trader's credit tracking could affect range-trader monitoring.

5. **Launchd timing**: The PRD specifies 7200 seconds (2 hours) for the log monitor, but the base-trader monitor also runs every 2 hours. Consider staggering to avoid simultaneous Telegram API calls.

6. **Recommendation**: Implement stories in order: US-001 (verifier fix) → US-002 (log monitor) → US-003 (launchd) → US-004 (daily summary). Each builds on the previous for operational visibility.

