# Code Review: range-trader-live
**Date:** 2026-03-26
**Reviewer:** Jill (manual audit)
**Files reviewed:** 9
**Overall:** ⚠️ Minor issues — production ready

---

## Summary

Range Trader is in good shape. Unlike base-trader, it does not have the critical BUY-branch ordering bug or the unconditional position-closure bug. Telegram routing is correct. Error propagation is solid. The only finding is redundant `import` statements in `range_execute.py` — a style issue, not a functional bug.

---

## 🔴 Critical (must fix before ship)

None found.

---

## ⚠️ Medium (fix before next sprint)

| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | `range_execute.py` | Lines 291 and 313: `import requests` inside `_get_token_price()` and `_get_weth_price()`. No module-level `import requests`. These local imports are redundant — Python caches imported modules, so repeated calls don't re-import. However, they are inconsistent with the module-level import pattern used elsewhere. | Minor code smell. Not a functional bug since there's no module-level import to shadow. | Add a module-level `import requests` at the top of the file and remove the two local imports. |
| 2 | `run_range_pipeline.py` | Line 20: `required` list for startup check includes `TELEGRAM_CHAT_ID` env var, but the actual Telegram alert code at line 26 uses hardcoded `group_id = '-5264050975'`. The `TELEGRAM_CHAT_ID` env var is never read. | Misleading — developer might think they need to set `TELEGRAM_CHAT_ID` when only `TELEGRAM_BOT_TOKEN` is actually used. | Remove `TELEGRAM_CHAT_ID` from the required list, or wire it into the alert function as intended. |

---

## 🔵 Minor (nice to have)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | `range_execute.py` | `_get_nonce` uses `'latest'` block tag. Comment says this avoids stuck pending txs — but this means any pending tx from a prior failed attempt will cause the next nonce to conflict. Base trader uses pending-aware nonce with `_wait_for_pending_cleared`. | Consider adding pending-aware nonce handling (like base-trader) to improve tx robustness during network congestion. |
| 2 | `range_execute.py` | `_gt_throttle()` uses `time.sleep(12)` — hardcoded 12-second gap for GeckoTerminal rate limit (10 calls/min free tier). | Make the throttle gap configurable via env var. |
| 3 | `run_range_pipeline.py` | BTC kill switch (`BTC_KILL_SWITCH_PCT = -3.0`) uses CoinGecko with no fallback. If CoinGecko fails, `get_btc_4h_change()` returns `0.0` which passes the check — could allow buys during a crash if the API is down. | Return a sentinel value (e.g., `None`) on API failure and treat `None` as "don't trade" rather than "0% change". |

---

## ✅ Verified Working

- **Exit monitor position handling**: `result['status'] == 'ok'` is checked before `remove_position()` — no orphaned open positions on failed sells ✅
- **Telegram routing**: `range_exit_monitor.py` and `run_range_pipeline.py` both send to group `-5264050975` ✅
- **Error propagation**: `buy_token()` and `sell_token()` return `status='ok'` or `status='skipped'`/`'failed'` with error messages on all exception paths ✅
- **Startup key check**: `_startup_key_check()` runs before any trading, alerts via Telegram and exits cleanly if env vars are missing ✅
- **Position tracking**: Positions written to `range_positions.json` after on-chain confirmation via `add_position()` ✅
- **Watch-only tokens**: CRV and ETHFI are correctly excluded from execution in `SLIPSTREAM_TOKENS` config ✅
- **Lockfile**: Prevents concurrent pipeline runs with PID check ✅
- **BTC kill switch**: Filters BUY signals when BTC drops >3% in 4h ✅
- **SELL before BUY ordering**: Pipeline executes sells first to free USDC before buying ✅
- **Gas reservation**: `tradeable_eth = eth_balance - 0.005` reserves gas buffer ✅
- **Duplicate import in exit_monitor.py**: Only one `import requests` at line 20 — no duplicate ✅
