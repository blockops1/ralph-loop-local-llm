```markdown
# Critique: US-004 - Implement range_engine.py - BB+ATR+ADX signal engine
**Reviewed:** 2024-01-15
**Files changed:** 1
**Overall verdict:** 🔴 Major issues

---

## ✅ What's Good
- `calculate_bb` implementation is correct with proper SMA, std_dev, and bandwidth calculations
- `calculate_tr` correctly implements the True Range formula
- `calculate_atr` properly computes SMA of TR values over the period
- `calculate_rsi` follows standard RSI calculation with proper gain/loss averaging
- `get_range` has good orchestrator structure with clear signal logic flow
- Documentation is thorough with helpful docstrings throughout

---

## ⚠️ Improvements (low risk)
### Unused math import
**File:** range_engine.py
**Issue:** PRD mentions `math` import but it's not used. `statistics` module could simplify std_dev calculation.
**Suggestion:** Either add `import math` and use `math.sqrt()` or use `statistics.stdev()` for cleaner code.
**Priority:** low

### calculate_dm helper function
**File:** range_engine.py
**Issue:** `calculate_dm` function exists but isn't in the PRD requirements. It's used internally by `calculate_dx`.
**Suggestion:** Keep as internal helper but consider prefixing with underscore (`_calculate_dm`) to indicate it's private.
**Priority:** low

### Default config fallback
**File:** range_engine.py
**Issue:** Line 365-367 falls back to 'WETH/USDC' config if pair not found, which could mask configuration errors.
**Suggestion:** Raise a clear error or return SKIP signal with explicit reason when pair config is missing.
**Priority:** low

---

## 🔴 Must Rework (high risk or clearly wrong)
### calculate_dx API signature mismatch
**File:** range_engine.py
**Issue:** PRD specifies `calculate_dx(pos_dm: float, neg_dm: float, tr: float) -> float` but implementation has `calculate_dx(candles: list, period: int = 14) -> float`. This is a complete API mismatch that will break tests.
**Impact:** Tests in test_range_engine.py will fail because they expect the simpler signature with three float parameters, not a candles list.
**Priority:** high

### calculate_adx uses SMA instead of EMA
**File:** range_engine.py
**Issue:** PRD explicitly states "Compute DX, then EMA(DX, period) = ADX" but implementation uses SMA (lines 245-255). ADX requires exponential moving average, not simple moving average.
**Impact:** ADX values will be mathematically incorrect, causing regime detection to fail and tests to fail.
**Priority:** high

### Missing synth_candles function
**File:** range_engine.py
**Issue:** PRD explicitly requires "Synthetic data helpers for tests" with `synth_candles(n: int, start_price: float, trend: str) -> list[dict]`. This function is completely missing.
**Impact:** Tests that rely on synthetic data generation will fail.
**Priority:** high

### get_range uses wrong candles for ADX
**File:** range_engine.py
**Issue:** PRD step 4c says "Calculate ADX(14) on candles_4h" but line 372 calculates `calculate_adx(candles_1h, ...)`. ADX should be computed on 4-hour candles, not 1-hour.
**Impact:** Regime detection will be based on wrong timeframe data, causing incorrect signals.
**Priority:** high

### calculate_dx formula implementation
**File:** range_engine.py
**Issue:** PRD specifies DX formula uses EMA for DI calculations: `di_plus = 100 * EMA(pos_dm/tr, period=14)`. Implementation uses SMA for smoothing (lines 207-213).
**Impact:** DX values will be mathematically incorrect, cascading to ADX errors.
**Priority:** high

---

## 🗑️ Deletion Candidates
Files or functions that appear unnecessary:
- `calculate_dm` function (lines 163-187): Could be inlined into `calculate_dx` since it's only used once and not part of the public API.

---

## Rework Stories Suggested
- [ ] Fix calculate_dx signature to match PRD (pos_dm, neg_dm, tr parameters) → target: range_engine.py
- [ ] Implement EMA helper function for ADX/DX calculations → target: range_engine.py
- [ ] Add synth_candles function for test data generation → target: range_engine.py
- [ ] Fix get_range to use candles_4h for ADX calculation → target: range_engine.py
```

```python
# Summary of critical issues found:
# 1. calculate_dx has wrong signature (candles vs pos_dm/neg_dm/tr)
# 2. ADX uses SMA instead of required EMA
# 3. Missing synth_candles function
# 4. get_range uses candles_1h instead of candles_4h for ADX
# 5. DX formula uses SMA instead of EMA for DI smoothing
```