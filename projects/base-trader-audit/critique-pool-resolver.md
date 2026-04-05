# Jill's Critique — pool_resolver.py Integration (2026-03-27)

## Files Changed
- `pool_resolver.py` — NEW singleton DexScreener caller
- `execute.py` — replaced `find_aerodrome_pool()` with `get_pool_info()` + `resolve_single()` fallback
- `scanner.py` — replaced `_enrich_liquidity_dexscreener()` with `_enrich_liquidity_pool_resolver()`
- `gecko_terminal.py` — replaced `_dexscreener_get_price()` with `_pool_cache_price_fallback()`
- `run_pipeline.py` — calls `resolve_pools()` after discovery

## Bugs Found & Fixed

### P0: resolve_single() didn't write failed entries to cache
**File:** `pool_resolver.py`
**Problem:** When `_dexscreener_lookup()` returned `None` (no Aerodrome pool), `resolve_single()` returned `None` without writing any entry to the cache. This meant:
- `get_pool_info()` returned `None` for that token on subsequent calls
- No `status: failed` entry existed, so retry TTL logic never applied
- Every call to `resolve_single()` for the same token would make the same live DexScreener call

**Fix:** Write a `status: failed` entry to cache when lookup fails, same as `resolve_pools()` does.

## Architecture Notes for Ralph

1. **DexScreener singleton pattern**: All DexScreener calls now route through `pool_resolver.py`. The cache file `data/pool_cache.json` is permanent — resolved pools are never re-queried.

2. **Cache TTL**: Only applies to failed resolutions (10 min). Successful resolutions are permanent.

3. **Fallback chain**: `execute.py` uses `get_pool_info()` (instant cache read) → `resolve_single()` (live call if cache miss). `scanner.py` and `gecko_terminal.py` use the same fallback chain.

4. **Rate limiting**: `resolve_pools()` has `time.sleep(0.25)` between DexScreener calls to avoid rate limiting.

5. **Atomic writes**: `_save_cache()` uses `tmp.replace()` for atomic disk writes.

## What to Focus On
- Is the DexScreener singleton pattern implemented correctly throughout?
- Are there any race conditions in cache reads/writes?
- Is the failed-entry TTL logic correct in all code paths?
- Are there any edge cases where `execute.py` could proceed with a swap without a valid pool?
