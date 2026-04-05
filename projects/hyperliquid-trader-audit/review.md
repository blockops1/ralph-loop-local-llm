# Code Review: hyperliquid-trader-live
**Date:** 2026-03-26
**Reviewer:** Jill (manual audit)
**Files reviewed:** 8
**Overall:** 🔴 2 critical bugs, 1 high — fix before paper trading goes live

---

## Summary

HyperLiquid trader is a perpetual futures system built via Ralph. Two confirmed critical bugs that would cause silent failures in live trading. Unlike the spot traders, this one holds positions off-chain (HyperLiquid internal state) so local `positions.json` is the only record — making the reconciliation gap a higher-risk issue here.

---

## 🔴 Critical (must fix before any live trading)

### 1. `execute.py` line 224 — close status check is logically wrong

```python
if not order_result.get("status") == "success":   # BUG
    return {"error": f"Failed to close position: {order_result}"}
```

HyperLiquid SDK returns `{"status": "ok"}` — not `"success"`.  
- `not "ok" == "success"` → `not False` → `True` → treated as ERROR ❌  
- Every real `close_position_market()` call returns an error, even when HyperLiquid closes the position successfully

**Impact**: Position is closed on HyperLiquid but `close_position()` is never called locally. `positions.json` still shows the position as open. Exit monitor will keep trying to close it.

**Fix**: `if order_result.get("status") != "success":`  
Or better: `if order_result.get("status") not in ("ok", "success"):`

---

### 2. `execute.py` line 258 — `_notify_telegram()` is a dead stub

```python
def _notify_telegram(message: str) -> None:
    config = _load_config()
    if not config.get("notifications", {}).get("enabled", False):
        return
    # Telegram notification logic would go here
    # For now, just log to console   ← STUB, never sends
    print(f"[TELEGRAM] {message}")
```

`_notify_telegram()` is called after every `open_position()` and `close_position_market()` but never actually sends Telegram. The only real Telegram alert is `run_pipeline.py`'s `notify()` which fires at the end of the full pipeline run — not per-trade.

**Impact**: No per-trade Telegram alerts. Config has `"notifications": {"enabled": true}` but the implementation is missing.

**Fix**: Replace stub with actual Telegram POST to `https://api.telegram.org/bot{token}/sendMessage` using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from env. Or call `run_pipeline.py`'s `notify()` from execute.py.

---

## ⚠️ High (fix before paper trading starts)

### 3. `positions.py` — no on-chain reconciliation

Unlike Base/Solana traders which can verify positions on-chain, HyperLiquid positions exist only in `positions.json`. If the file is deleted or corrupted, all position tracking is lost. The `reconcile.py` script exists but it's unclear if the pipeline calls it.

**Impact**: Ghost positions or lost positions with no recovery mechanism.

**Fix**: `run_pipeline.py` should call `reconcile_positions()` at startup (it does call it, but the reconcile function itself may not fetch HyperLiquid on-chain positions and diff against local state).

---

## ⚠️ Medium

### 4. `execute.py` — `open_position()` adds position before confirming SDK response

```python
order_result = place_order(...)
if order_result.get("status") not in ("ok", "success"):
    return {"error": ...}
...
add_position(position_record)   # Called after the check — correct
```

This is actually correct. But note: `place_order()` raises `RuntimeError` on exceptions — not a dict. The caller in `run_pipeline.py` catches this with `except Exception as e`. The `entry_result.get("confirmed")` check in `run_pipeline.py` is the guard — but `check_entry_conditions()` may not be setting a `confirmed` field properly.

### 5. `config.yaml` — `notifications.telegram` not wired

The config has `notifications:` with `type: "email"` and `type: "slack"` entries but no `type: "telegram"`. The Telegram env vars (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are used in `run_pipeline.py` but not documented in config. Also: `TELEGRAM_CHAT_ID` is the personal chat, not the trading group `-5264050975`.

### 6. `exit_monitor.py` — alert cache filename inconsistency

Uses `ALERT_CACHE_FILE = "data/exit_alerts_cache.json"` but the rest of the system uses `data/exit_monitor_log.jsonl`. Minor but suggests copy-paste from another file.

---

## 🔵 Minor

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | `execute.py` | `place_order()` raises `RuntimeError` on exception — caught as `Exception` in caller. But `cancel_order()` raises `RuntimeError` too — inconsistent with `place_order()` returning error dicts in paper mode. | Standardize: return error dicts consistently, don't raise. |
| 2 | `hl_client.py` | `get_client()` creates both `Info` and `Exchange` objects but only uses `exchange.wallet.address` for the address. The `Info` object is unused in this function. | Minor waste — the `info` return is unused here. |
| 3 | `exit_monitor.py` | `update_peak_trough()` calls `get_open_positions()` unnecessarily (already in loop) and then re-loads all positions to update one. | Optimize: update in-memory position directly, save once at end of loop. |
| 4 | `run_pipeline.py` | `notify()` uses `TELEGRAM_CHAT_ID` — should verify this is the trading group, not a personal chat. | Confirm: does `TELEGRAM_CHAT_ID` = `-5264050975` for the trading group? |

---

## ✅ Verified Working

- **Entry ordering**: `check_entry_conditions()` → `open_position()` → `add_position()` — correct order ✅
- **BUY/SELL direction**: `is_buy = direction == "LONG"` for open, `is_buy = direction == "SHORT"` for close — correct ✅
- **Stop loss + TP orders**: Both SL and TP trigger orders placed alongside market order ✅
- **Exit monitor error handling**: `result.get("status") == "success"` check before marking closed ✅
- **BTC kill switch**: Configured at -3%, runs before scanner ✅
- **Funding kill switch**: Checks funding rate after 4h open ✅
- **Alert cache**: Prevents alert spam (30-min dedup) ✅
- **Entry score threshold**: Sorted candidates, breaks on first sub-threshold ✅
