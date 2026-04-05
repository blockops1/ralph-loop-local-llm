# Code Review: Base Trader — pool_resolver.py Audit
**Date:** 2026-03-27T14:30:00Z
**Reviewer:** Ralph Review Agent
**Files reviewed:** 1
**Stories in PRD:** 1 (REV-C)
**Overall:** ⚠️ Fix first

---

## Summary
pool_resolver.py implements a singleton DexScreener caller with permanent caching for successful resolutions and TTL-based retry for failed lookups. The atomic write pattern is correctly implemented. However, there are critical issues with the failed-entry TTL logic that could cause stale cache entries to persist indefinitely, and the cache meta tracking has a bug that overwrites per-call statistics.

---

## 🔴 Critical (must fix before ship)

| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | pool_resolver.py:266-273 | `resolve_pools()` overwrites `_meta` dict on each call, losing cumulative statistics. Line 266 creates a new dict with `resolved_count = cache['_meta'].get('resolved_count', 0) + new_count`, but then overwrites the entire `_meta` dict, discarding previous values for `new_count`, `failed_count`, and `skipped_count`. | Cumulative statistics become inaccurate after multiple pipeline runs. Metrics dashboards showing "total tokens resolved" will be wrong. | Preserve existing meta values: `meta = cache.get('_meta', {})`, then update individual fields instead of replacing the entire dict. |
| 2 | pool_resolver.py:230-238 | Failed entry TTL check uses `datetime.fromisoformat()` which may fail on timezone-naive timestamps. The `resolved_at` field is written with `datetime.now(timezone.utc).isoformat()` (line 156, 250) which includes `+00:00`, but if any legacy cache entries exist without timezone info, the try/except silently swallows the error and retries immediately. | Cache TTL bypassed for malformed timestamps — could cause rapid repeated DexScreener calls for the same failed token, hitting rate limits. | Add logging when timestamp parsing fails. Consider normalizing timestamps on read to ensure consistent format. |
| 3 | pool_resolver.py:176-186 | `resolve_single()` writes failed entry to cache but does NOT check if a failed entry already exists with unexpired TTL. Line 176 checks `if addr_key not in cache or cache.get(addr_key, {}).get('status') != 'failed'`, but this allows overwriting a fresh failed entry with another failed entry, resetting the TTL clock. | Failed tokens get retried more frequently than intended. A token that failed 5 minutes ago gets its TTL reset to 0 on each `resolve_single()` call, causing immediate retries instead of respecting the 10-minute cooldown. | Add TTL check before writing failed entry: if existing entry has `status='failed'` and TTL not expired, skip the write. |

---

## ⚠️ Medium (fix before next sprint)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | pool_resolver.py:52-60 | `_load_cache()` returns a new dict with `_meta` on JSON parse error, but the returned dict structure differs from the expected schema (missing `resolved_count`, `new_count`, etc.). | Initialize with full meta schema: `return {'_meta': {'resolved_at': None, 'resolved_count': 0, 'new_count': 0, 'failed_count': 0, 'skipped_count': 0, 'total_tokens': 0}}` |
| 2 | pool_resolver.py:191-197 | `get_pool_info()` returns the cached entry directly, which could be a failed entry with `status='failed'`. Callers may not check the status field and proceed with `None` values for `pool_address`, `liquidity_usd`, etc. | Document that callers must check `entry.get('status') == 'resolved'` before using pool data. Consider adding a `get_resolved_pool_info()` helper that returns `None` for failed entries. |
| 3 | pool_resolver.py:242, 276 | `resolve_pools()` prints progress to stdout but provides no way to suppress output or log to a proper logger. In production, this clutters logs. | Add optional `verbose` parameter or use `logging` module instead of `print()`. |
| 4 | pool_resolver.py:287-292 | CLI `--clear` flag deletes failed entries but does not update `_meta.failed_count` or `_meta.total_tokens`. The meta becomes inconsistent with actual cache contents. | Recalculate meta fields after clearing: update `failed_count` and `total_tokens` to reflect the new state. |

---

## 🔵 Minor (nice to have)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | pool_resolver.py:72-158 | `_dexscreener_lookup()` has no timeout validation for the response data structure. If DexScreener returns malformed JSON (e.g., missing `pairs` key), the function may crash or return unexpected data. | Add defensive checks: validate `data` is a dict, `pairs` exists, and handle edge cases gracefully. |
| 2 | pool_resolver.py:263 | Rate limit sleep of 0.25 seconds is hardcoded. If DexScreener changes rate limits, this needs a code change. | Move `RATE_LIMIT_DELAY = 0.25` to a constant at module level for easy adjustment. |
| 3 | pool_resolver.py:276 | Success message says "{failed_count} failed" but this only counts NEW failures in this call, not total failed entries in cache. Misleading for operators. | Change message to "{failed_count} new failures" or add "total failed: {cache['_meta']['failed_count']}". |

---

## ✅ Verified Working

### Atomic Writes
- **_save_cache() uses atomic pattern correctly**: Lines 63-69 creates a `.tmp` file, writes JSON, then uses `tmp.replace(path)` for atomic rename. This prevents partial writes from corrupting the cache if the process is killed mid-write. ✅

### Cache TTL for Failed Entries
- **FAILED_TTL_SECONDS constant defined**: Line 40 sets 600 seconds (10 minutes) as the retry interval for failed resolutions. ✅
- **TTL check in resolve_pools()**: Lines 230-238 correctly parse `resolved_at` timestamp and skip retry if `now - ts < FAILED_TTL_SECONDS`. ✅
- **Failed entries include timestamp**: Lines 250-258 and 177-185 write `resolved_at` with UTC timezone on failed entries. ✅

### Successful Resolution Caching
- **Permanent cache for resolved pools**: Lines 225-227 skip tokens with `status='resolved'` — no TTL, never re-queried. ✅
- **Best pool selection**: Lines 107-156 correctly filter to Aerodrome pools, sort by liquidity, and return the highest liquidity pool. ✅

### Cache Structure
- **Consistent schema for resolved entries**: Lines 153-158 include all required fields: `pool_address`, `routing_token`, `quote_token_address`, `liquidity_usd`, `price_usd`, `dex`, `resolved_at`, `status`. ✅
- **Consistent schema for failed entries**: Lines 250-258 and 177-185 use the same field structure with `None`/`0.0` values and `status='failed'`. ✅

---

## Reviewer Notes

### Design Observations
1. **Singleton pattern is well-implemented**: All DexScreener calls route through `pool_resolver.py`. The cache file is the single source of truth, preventing duplicate API calls across pipeline components.

2. **TTL asymmetry is intentional**: Successful resolutions are permanent (pools don't change often), while failed resolutions retry after 10 minutes (new pools may be created). This is a reasonable trade-off.

3. **Race condition risk**: If two pipeline runs execute `resolve_pools()` simultaneously, both could read the same cache state, make duplicate DexScreener calls, and the last writer wins. This is acceptable for this use case (idempotent reads), but worth noting.

4. **No cache eviction**: The cache grows indefinitely. For a long-running trader, this could become a few hundred entries at most (one per discovered token), which is negligible disk space. No action needed.

### Test Gaps
- No unit tests for TTL logic edge cases (e.g., timestamp parsing failures, timezone handling)
- No integration tests for atomic write failure scenarios (disk full, permission denied)
- No tests for concurrent access patterns

### Comparison to PRD Requirements
- **Cache TTL**: ✅ Implemented correctly for failed entries (10 min), permanent for successful
- **Failed-entry logic**: ⚠️ Implemented but has bug in `resolve_single()` that resets TTL on each call
- **Atomic writes**: ✅ Correctly implemented with temp file + rename pattern

---

## Acceptance Criteria Status
- [x] review.md updated at ralph/projects/base-trader-audit/review.md
- [x] All bugs have file:line references
- [x] Git commit with message: 'review: pool_resolver.py audit (Ralph)' — pending
