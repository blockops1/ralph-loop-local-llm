# PROJ: Base Trader Nansen Decoupling

**Business:** horizen-zkverify
**Created:** 2026-04-04
**Status:** DRAFT — awaiting go-ahead
**Owner:** Mr. V + Jill

---

## WHY

The Nansen `flow-intelligence` API call inside `scanner.py` has been burning 10 credits per token per run for months — returning all zeros because the response fields don't match what the code expects. That's ~1,000 dead credits per day with zero signal value.

Two structural problems:
1. **Tight coupling:** The pipeline calls Nansen directly on every run. No throttle, no schedule control, no way to accumulate time-series data.
2. **Wrong endpoint model:** The code expects timeframe-keyed fields (`net_flow_1h_usd`, `net_flow_24h_usd`) but Nansen returns segment-keyed fields (`smart_trader_net_flow_usd`, `whale_net_flow_usd`) for a single timeframe per call.

Decoupling fixes both: a standalone fetcher runs on its own schedule, accumulates snapshots, and the pipeline just reads from local cache.

---

## ARCHITECTURE

```
[Nansen API]                 [Standalone Fetcher]           [Local Cache]              [Pipeline]
  POST /api/v1/tgm/   -->   fetch_nansen_flow.py    -->   flow_cache.json    -->   scanner.py
  flow-intelligence           (cron: 1x/day per token)        (time series)              reads only
  (10 cr/call)                accumulates snapshots
                               computes rolling windows
```

**Key principle:** Pipeline is a consumer. It never calls Nansen.

---

## WHAT

### Part 1: Standalone Fetcher (`scripts/fetch_nansen_flow.py`)

**Schedule:** Hermes cron, runs 1×/day at ~00:05 ET (low-traffic, fresh day boundary)

**Token list:** Reads tracked tokens from `data/sm_holdings_cache.json` (existing). If empty, falls back to hardcoded watchlist in `config/watchlist.json`.

**Logic per token:**

```
1. Check throttle: if last_fetched_[token] < 1 hour ago, skip (0 credits)
2. Call Nansen:
   POST https://api.nansen.ai/api/v1/tgm/flow-intelligence
   Headers: { x-api-key: ..., Content-Type: application/json }
   Body: { token_address: addr, chain: 'base', timeframe: '1d' }
3. Parse response:
   total_net = sum of all *_net_flow_usd fields
   snapshot = {
     ts:          UTC now,
     total_net_flow_usd:         total_net,
     smart_trader_net_flow_usd: data.smart_trader_net_flow_usd,
     whale_net_flow_usd:         data.whale_net_flow_usd,
     exchange_net_flow_usd:      data.exchange_net_flow_usd,
     public_figure_net_flow_usd: data.public_figure_net_flow_usd,
     top_pnl_net_flow_usd:       data.top_pnl_net_flow_usd,
     fresh_wallets_net_flow_usd: data.fresh_wallets_net_flow_usd,
     smart_trader_wallets:       data.smart_trader_wallet_count,
     whale_wallets:              data.whale_wallet_count,
     exchange_wallets:           data.exchange_wallet_count,
   }
4. Append snapshot to cache entry for this token
5. Prune: remove snapshots older than 30 days
6. Log: tokens updated, credits spent, errors
```

**Throttle mechanism:** Simple file-based flag — write `last_fetched_[token] = now` to a thin index. On next run, skip if < 1h elapsed.

**Budget guard:** Before calling, check `credit_tracker` — if daily budget exhausted, log and skip all tokens.

**Output:** Updates `data/flow_intelligence_cache.json` only. Sends Telegram summary: tokens updated, credits used, any errors.

---

### Part 2: Updated `scanner.py` (`get_flow_intelligence()`)

**Change:** Remove Nansen API call entirely. Replace with cache reader.

**New logic:**

```
1. Load flow_intelligence_cache.json
2. Get all snapshots for this token
3. Compute rolling windows from time series:
   - net_flow_1h:  sum of snapshots from last 1 hour
                   (if <1h of history: use most recent single snapshot's total)
   - net_flow_24h: sum of snapshots from last 24 hours
   - net_flow_7d:  sum of snapshots from last 7 days
   - net_flow_30d: sum of snapshots from last 30 days
4. Buy/sell pressure: from most recent snapshot
5. Return flat dict (same shape signal_filter.py expects)
6. On cache miss or empty: return safe zeros, log debug warning
```

**Cold start (< 24h of history):** `net_flow_24h` falls back to Nansen's own 1d value. After 7 days, rolling 7d is computed from accumulated hourly snapshots — Nansen's 7d call no longer needed.

---

### Part 3: New Cache Format (v3 — time series)

`data/flow_intelligence_cache.json`:

```json
{
  "_index": {
    "last_full_fetch": "2026-04-04T00:05:00Z",
    "tokens": ["0x1234...", "0x5678...", "0xabcd..."]
  },
  "tokens": {
    "0x1234...": {
      "last_fetched": "2026-04-04T12:30:00Z",
      "timeframe": "1d",
      "snapshots": [
        {
          "ts": "2026-04-04T12:30:00Z",
          "total_net_flow_usd": 125000.0,
          "smart_trader_net_flow_usd": 50000.0,
          "whale_net_flow_usd": 30000.0,
          "exchange_net_flow_usd": 20000.0,
          "public_figure_net_flow_usd": 15000.0,
          "top_pnl_net_flow_usd": 8000.0,
          "fresh_wallets_net_flow_usd": 2000.0,
          "smart_trader_wallets": 15,
          "whale_wallets": 8,
          "exchange_wallets": 3
        },
        {
          "ts": "2026-04-03T12:30:00Z",
          "total_net_flow_usd": 98000.0,
          ...
        }
      ]
    }
  }
}
```

**Prune rule:** On each fetch cycle, remove all snapshots older than 30 days. Run prune once per cycle, not per token.

---

## CREDIT COST

| Phase | Tokens | Calls | Credits |
|-------|--------|-------|---------|
| Cold start (manual seed) | 10 | 10 | 100 (one-time) |
| Daily fetch (1×/day) | 10 | 10 | 100/day |
| Hourly fetch (1×/hour, optional) | 10 | 10/hour | 2,400/day |

**Recommendation:** Daily at first. After 7 days of accumulated hourly snapshots, rolling 7d windows are computable from your own data — Nansen 7d call becomes unnecessary.

---

## FILES CHANGED

|| File | Change |
||------|--------|
| New | `scripts/fetch_nansen_flow.py` | Standalone fetcher, cron-driven |
| New | `config/watchlist.json` | Fallback token list if holdings cache is empty |
| Modified | `scripts/scanner.py` | `get_flow_intelligence()` becomes cache reader only |
| Modified | `data/flow_intelligence_cache.json` | Format upgraded from flat dict to time series |

**No changes to:** `signal_filter.py`, `run_pipeline.py`, `credit_tracker.py`, `decision.py`

---

## DERIVED SIGNALS (future)

Once the time series has 14+ days of history, new signals become available from the segment data:

| Signal | Formula | Use |
|--------|---------|-----|
| `smart_money_flow_pct` | smart_trader_net / total_net | Fraction of flow from SM vs other segments |
| `exchange_flow_pct` | exchange_net / total_net | High = distribution pressure |
| `flow_velocity` | net_flow_24h / net_flow_7d | >1 = accelerating, <1 = decelerating |
| `sm_conviction` | smart_trader_net > 0 AND wallets >= 5 | Boolean conviction signal |
| `whale_loading` | whale_net_flow_24h > threshold | Whale accumulation signal |

These don't exist in Nansen's output — they're ours to compute from stored segment data.

---

## STEPS

1. **Write `fetch_nansen_flow.py`** — standalone fetcher with throttle, budget guard, Telegram summary
2. **Update `scanner.py`** — swap `get_flow_intelligence()` from API caller to cache reader with rolling window computation
3. **Manual seed run** — run fetcher once to populate initial cache (~100 credits)
4. **Update PROJ-base-trader-improvements.md** — mark SM2 (flow intelligence fix) as complete with new implementation
5. **Set up cron** — Hermes cron, daily at 00:05 ET
6. **Monitor** — verify cache grows, pipeline scores change, credits stay within budget

---

## ACCEPTANCE CRITERIA

- [ ] `fetch_nansen_flow.py` runs without errors
- [ ] `flow_intelligence_cache.json` contains time-series snapshots after seed run
- [ ] `scanner.py` returns non-zero netflow values (not all zeros)
- [ ] `signal_filter.py` receives populated `sm_netflow` dict
- [ ] Credit usage: ~100 credits/day for daily fetch
- [ ] Pipeline runs without calling Nansen API directly
- [ ] Telegram summary sent after each fetch run

---

## RALPH PROJECT

**Slug:** `bt-nansen-decouple`
**PRD:** To be created in `ralph/projects/bt-nansen-decouple/prd.json`
