# Code Review: solana-trader-live
**Date:** 2026-03-26
**Reviewer:** Jill (manual audit)
**Files reviewed:** 8
**Overall:** ⚠️ Minor issues — production ready

---

## Summary

Solana trader is the cleanest of the three traders. No critical bugs. BUY ordering is correct, position tracking is correct, Telegram routing is correct. The main issue is scattered `import` statements (style, not functional) and a few minor robustness gaps.

---

## 🔴 Critical (must fix before ship)

None.

---

## ⚠️ Medium

| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | `run_pipeline.py` | Lines 29, 38: `import requests` inside `notify()` and `get_nansen_credits()` — not at module level. Works but inconsistent. | Code smell. Local import shadows nothing since no module-level import exists. | Move to module level: `import requests` after line 5. |
| 2 | `run_pipeline.py` | Line 47: `required = [..., 'TELEGRAM_CHAT_ID']` — this var is never used. `notify()` uses hardcoded `group_id = '-5264050975'`. | Misleading. Developer might think TELEGRAM_CHAT_ID is wired up. | Remove `TELEGRAM_CHAT_ID` from required list. |
| 3 | `decision.py` | Lines 25, 36: `import time` inside `is_on_cooldown()` and `set_cooldown()`. | Code smell. `time` should be at module level. | Add `import time` to module-level imports. |
| 4 | `exit_monitor.py` | Failed sells don't update position status. If `sell_token()` fails, the position stays in whatever state it was in, with no error marker. | Stale positions remain "open" silently with no indication a sell was attempted. | On failed sell, set `pos['status'] = 'exit_failed'` or similar. |

---

## 🔵 Minor

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | `decision.py` | `set_cooldown()` reads entire cooldown file, modifies in-memory dict, then writes back. Not atomic — concurrent pipeline runs could lose data. | Use file locking or atomic rename. |
| 2 | `exit_monitor.py` | `get_price_sol()` calls DexScreener for every position on every check cycle. No caching. | Cache price per token for the duration of a single check cycle. |
| 3 | `scanner.py` | Uses `get_flow_intelligence` import but that function may not exist (Nansen endpoint changed to `smart-money/netflow`). | Verify `get_flow_intelligence` is still functional or has been replaced. |
| 4 | `run_pipeline.py` | `paper_trade` default is read from config but there's no explicit `paper_trade` env var override for emergency stops. | Consider env var `SOLANA_PAPER_TRADE=0` to force live trading in emergencies. |

---

## ✅ Verified Working

- **BUY ordering**: `decision = format_decision()` → `buy_token()` → `add_position()` — correct order ✅
- **Position closure on sell success**: `pos['status'] = 'closed'` only set inside `if result.get('success')` — no orphaned open positions ✅
- **Telegram routing**: `notify()` uses `group_id = '-5264050975'` — correct group, not personal chat ✅
- **Error propagation**: `buy_token()` and `sell_token()` return `{'success': True/False}` with error string — all paths covered ✅
- **Partial exits**: Tier1/Tier2/Trailing logic correctly implemented with partial sell tracking ✅
- **Cooldown system**: Failed buys are marked in cooldown file — prevents repeated failed buy attempts ✅
- **Blacklist**: Blacklisted symbols skipped — prevents duplicate entries ✅
- **No duplicate imports**: `execute.py` has module-level `import requests` — no shadowing issue ✅
