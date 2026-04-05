# Critique: Base Trader Full Audit
**Reviewed:** 2026-03-26  
**Files changed:** 4  
**Overall verdict:** ⚠️ Needs work

---

## ✅ What's Good
- Partial exit feature appears implemented and logged correctly (AERO 50% exit at target hit, `trade_log.jsonl` shows `PARTIAL_EXIT` event with correct fields)
- Position state tracking is working — `positions.json` shows closed positions with accurate `pnl_pct`, `close_reason`, and timestamps
- Exit monitor logic appears sound — stop loss and target hit conditions trigger as expected based on log output

---

## ⚠️ Improvements (low risk)

### Partial exit field inconsistency
**File:** `businesses/horizen-zkverify/base-trader/data/positions.json`
**Issue:** `trailing_stop` field appears on closed position but not on open positions (if any exist). The `partial_exit_at` field is also only on the closed position, not consistently applied across all positions.
**Suggestion:** Ensure all positions have consistent schema — either add these fields to all positions or document which positions have partial exits.
**Priority:** low

---

## 🔴 Must Rework (high risk or clearly wrong)

### Missing critical environment variables — production system running without security setup
**File:** `businesses/horizen-zkverify/base-trader/data/exit-monitor.log` (and `cron.log`)
**Issue:** Multiple `SELL failed: BASE_WALLET_PRIVATE_KEY not set in env` errors logged. The PRD explicitly requires `BASE_WALLET_ADDRESS` and private key for trades. The system is attempting to execute trades without the required credentials.
**Impact:** Trading system cannot execute exits in production. Positions may be stuck open without proper exit logic. This is a critical security and operational failure.
**Priority:** high

### Code review deliverable not produced
**File:** N/A (expected `review.md` not present in diff)
**Issue:** PRD acceptance criteria explicitly requires `review.md` written to `ralph/projects/base-trader-audit/review.md`. The diff shows only runtime data files (logs, JSON), not a code review document.
**Impact:** The audit task was not completed per acceptance criteria. Stakeholders have no documented findings from the code review.
**Priority:** high

### Telegram routing verification not evidenced
**File:** N/A (expected code review findings)
**Issue:** PRD requires verification that all Telegram alerts route to group `-5264050975` and not personal chat `374999219`. No code review output demonstrates this was checked.
**Impact:** If personal chat routing exists, it could leak trade signals to unintended recipients.
**Priority:** high

### Nonce handling not verified
**File:** N/A (expected code review findings)
**Issue:** PRD acceptance criteria requires nonce handling verification (pending block, `_wait_for_pending_cleared`, gas bump retry). No evidence this was reviewed.
**Impact:** If nonce handling is broken, trades could fail silently or cause replacement transaction errors, leading to lost funds or stuck positions.
**Priority:** high

---

## 🗑️ Deletion Candidates

- `businesses/horizen-zkverify/solana-trader/data/cron.log` — The Solana trader startup failure appears unrelated to Base Trader audit scope. If not actively maintained, this file may be noise.

---

## Rework Stories Suggested

- [ ] Complete code review and produce `review.md` with all findings → target: `ralph/projects/base-trader-audit/review.md`
- [ ] Fix missing environment variables for production trading (add to `.env` or add validation) → target: `businesses/horizen-zkverify/base-trader/.env`
- [ ] Add validation to ensure required env vars are set before attempting trades → target: `businesses/horizen-zkverify/base-trader/scripts/execute.py`
- [ ] Verify all Telegram routing code uses group `-5264050975` only → target: `businesses/horizen-zkverify/base-trader/scripts/run_pipeline.py`
- [ ] Audit nonce handling in `execute.py` for pending block and gas bump retry logic → target: `businesses/horizen-zkverify/base-trader/scripts/execute.py`