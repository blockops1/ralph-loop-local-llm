# PROJ-range-calculator-v2: Range Trading Engine Overhaul

**Business:** Trading competition — multi-pair, daily trades, 3-5 Aerodrome pairs
**Status:** Phase 4 ✅ COMPLETE — Phase 5 🔲 ON DECK (backtesting framework)

---

## Why

The current `support_resistance.py` uses a simple swing-point average (2 most recent highs/lows → S/R). This was adequate for a proof-of-concept but is insufficient for a trading competition where:

- Multiple pairs need solid range definitions simultaneously
- Range quality and width must be cost-adjusted (fees + slippage per pair)
- The system must filter trending vs ranging regimes (avoid fighting momentum)
- Static lookbacks fail across different market conditions

Grok's analysis makes clear: **Bollinger Bands + ATR + ADX regime filter** is the right architecture. This is a full overhaul, not an incremental patch.

---

## Scope

### In Scope
- New range calculation engine (BB + ATR + ADX hybrid)
- Data source research and upgrade if needed
- Multi-pair configuration (WETH/USDC, cbBTC/WETH, AERO/USDC, ZEN/WETH, VFY/USDC)
- Per-pair cost-adjusted minimum widths
- Backtesting framework with realistic slippage modeling
- Integration alongside existing `support_resistance.py` (parallel, not replace)
- Full pipeline integration test (dry-run)

### Out of Scope
- New exchange venues — Aerodrome only
- Pairs not on Aerodrome Base
- Live trading until Phase 6 (restart readiness)
- Modifying existing execution/logic (circuit breakers, nonce management, etc.)

---

## Phase 1 — Data Source Audit 🔲 COMPLETE

**Status:** Done. Findings below.

### Current OHLCV Flow (range_scanner.py)

| Data | Source | Granularity | Latency/Cache |
|------|--------|-------------|---------------|
| OHLCV candles | GeckoTerminal `ohlcv/day` | **Daily only** | Cached once per UTC day |
| Price USD | DexScreener (primary), GeckoTerminal (fallback) | Real-time | 5-min disk cache |
| Price ETH | DexScreener `priceNative` | Real-time | 5-min disk cache |
| Pool data | GeckoTerminal token pools | Real-time | Process lifetime cache |
| ETH price | DexScreener WETH lookup | Real-time | Per-run |

**GeckoTerminal rate limit:** 10 calls/min free. Throttled to 5 calls/min (`_GT_MIN_INTERVAL = 12s`).

### Critical Gap: No Intraday Candles

The entire BB + ATR + ADX framework requires **intraday candles** (1m to 4h) to be useful:
- **Bollinger Bands**: need 20+ periods of the target timeframe. Daily-only gives 60 data points (60 days) — sufficient for daily-range trading, but **useless for multiple daily trades**.
- **ATR**: same issue — daily ATR on 60 days of daily candles is slow to react.
- **ADX regime filter**: daily ADX changes direction over weeks, not usable for daily trade entries.
- **Multiple daily trades** require 1h or 4h candles minimum.

Current `support_resistance.py` **is not using intraday data at all** — it only gets daily candles, which is why it works for positional range trading but not for the competition use case (multiple daily trades).

### Data Source Decision

| Source | Granularity | Real-Time | Cost | Verdict |
|--------|-----------|-----------|------|---------|
| GeckoTerminal free | Daily only | No | Free | **Insufficient** — no intraday |
| CoinGecko paid | 1s candles + WebSocket | Yes | ~$39-100/mo | **Viable** — minimal code change |
| Bitquery | 1s OHLCV + tick streams | Yes (WS) | Points-based | **Best for Aerodrome** — native 1s data |
| The Graph | Block-level swaps | Near-realtime | Free → paid | Good for historical, weak for live |
| Alchemy/QuickNode RPC | Swap events | Yes | Existing RPC | Good for simulation, weak for OHLCV |

### Decision

**Upgrade data source before Phase 3 implementation.** Without intraday candles, the BB + ATR engine has no data to run on.

**Recommended:** Bitquery (Aerodrome-native, 1s candles, WebSocket streams) as primary. CoinGecko paid as alternative if Bitquery quota is tight.

**Rationale:**
- Bitquery gives 1-second OHLCV on Aerodrome pools — the exact granularity needed for precise BB/ATR on 1h and 4h timeframes
- WebSocket streams eliminate polling and rate limits
- Aerodrome-specific API means pool addresses, tick data, and liquidity all in one place
- Compatible with Python (graphql-request library)

### Action Required Before Phase 3
- [x] ~~Bitquery account + API token~~ (requires sales call — put on hold)
- [x] ZEN coin ID confirmed: `horizen` (was zencorpo — incorrect)
- [ ] CoinGecko Basic Plan signup + API key → add to `.env` as `COINGECKO_API_KEY`
- [ ] Test 1h candles for WETH/USDC (first live API call after signup)
- [ ] Expand to remaining pairs once WETH/USDC confirmed working

### CoinGecko Integration (Current Reality)
**Chosen over Bitquery** — Bitquery requires a sales call; CoinGecko has instant signup.

**What we know:**
- Endpoint: `GET /coins/{id}/ohlc?vs_currency=usd&days=1|7|30|90|...`
- Returns: `[[timestamp_ms, open, high, low, close], ...]` (no volume)
- 1h candles: `days=1` or `days=7` → 168 candles max
- 4h candles: aggregate 1h → 42 candles in 7 days
- Daily candles: `days=30/90/180/365/max`
- CoinGecko IDs: WETH=ethereum, AERO=aerodrome-finance, cbBTC=coinbase-wrapped-btc, ZEN=horizen, VFY=zkverify
- Rate limits: 30 calls/min (free), 500 calls/min (paid Basic ~$29/mo)

**Data layer:** `scripts/range_data.py` — written, needs CoinGecko API key to test

---

## Phase 1B — Data Source Design ⚠️ ARCHIVED (Bitquery → CoinGecko pivot)

**Status:** Superseded by CoinGecko integration above. Bitquery section kept as reference.

### API Details (researched)

**Endpoint:** `https://streaming.bitquery.io/graphql` (V2 API)
**Auth:** `X-API-KEY` header
**Docs:** `https://docs.bitquery.io/`

**Two relevant cubes for our use case:**

#### Cube 1: `Trading { Pairs }` — Pre-aggregated OHLC (recommended primary)
- Pool-specific OHLCV with any interval: 1s, 1m, 5m, 15m, 30m, 1h
- Real-time + recent history
- Filter by `Market.Address` (pool address) + `Market.Network` (base)

```graphql
subscription {
  Trading {
    Pairs(
      where: {
        Market: {
          Network: { is: "base" }
          Address: { is: "0x...AERODROME_POOL_ADDRESS..." }
        }
        Interval: { Time: { Duration: { eq: 3600 } } }  # 1h candles
        Price: { IsQuotedInUsd: true }
      }
      orderBy: { descendingByField: "Block_Time" }
      limit: { count: 100 }
    ) {
      Block { Date Time Timestamp }
      Interval { Time { Start Duration End } }
      Volume { Base Quote Usd }
      Price {
        IsQuotedInUsd
        Ohlc { Open High Low Close }
      }
    }
  }
}
```

**Supported intervals:** 1s, 3s, 5s, 10s, 30s, 1m, 5m, 15m, 30m, 1h

#### Cube 2: `EVM { DEXTradeByTokens }` — Historical backfill
- Raw swaps aggregated into candles by time bucket
- Use for historical backtesting (last 7+ days)
- Requires `dataset: combined` or `dataset: archive`

```graphql
{
  EVM(network: base, dataset: combined) {
    DEXTradeByTokens(
      limit: { count: 500 }
      orderBy: { ascending: Block_Time }
      where: {
        Trade: {
          Currency: { SmartContract: { is: "0x...TOKEN_ADDRESS..." } }
          Dex: { SmartContract: { is: "0x...AERODROME_FACTORY..." } }
        }
        Block: { Time: { since: "2026-03-01" } }
      }
    ) {
      Block { Time(interval: { count: 1, in: hours }) }
      Trade {
        open: PriceInUSD(minimum: Block_Number)
        high: PriceInUSD(maximum: Trade_PriceInUSD)
        low: PriceInUSD(minimum: Trade_PriceInUSD)
        close: PriceInUSD(maximum: Block_Number)
      }
      volumeUsd: sum(of: Trade_Side_AmountInUSD)
    }
  }
}
```

### Integration Architecture

**`range_data.py`** — new data layer for Bitquery:
```
get_candles_1h(pool_address, days=30) → list[dict]  # Trading API, hourly
get_candles_4h(pool_address, days=30) → list[dict]  # Trading API, 4h
get_candles_1m(pool_address, hours=4) → list[dict]  # Trading API, 1m (for live entries)
stream_candles(pool_address, interval) → WebSocket stream  # future enhancement
```

**Data flow:**
1. On scanner run: fetch 1h candles (last 30 days) + 4h candles for regime filter
2. Calculate BB + ATR on 1h timeframe
3. ADX regime filter on 4h timeframe
4. For live entries: fetch latest 1m candles to confirm touch

**Pool address discovery:**
- Aerodrome factory: `0x5ca0e5117f1491273d8d416a35c5d1b5f57d8f8f`
- Query `DEXTradeByTokens` by token address + factory to find pool address
- Cache pool addresses (they don't change often)

### Bitquery vs CoinGecko Decision

Bitquery wins because:
1. 1h/4h candles natively available via `Trading { Pairs }` — no aggregation needed
2. Aerodrome pool-specific data (not aggregated across all DEXs)
3. WebSocket subscriptions available for future real-time streaming
4. Points-based pricing — scales with actual usage

CoinGecko paid fallback: only if Bitquery quota exhausted or API changes significantly.

---

## Phase 2 — New Range Engine Architecture ✅ COMPLETE

**Goal:** Write the formal spec as `REFERENCES/range-engine-spec.md`. ✅ Done.

**Status:** BB + ATR + ADX formulas and per-pair parameters are designed (from Grok's analysis + our ZEN/VFY pair research). The remaining step is committing the design to a proper spec document.

### What we know (written into SPEC ✅)
1. [x] **Bollinger Bands**: period=20, standard deviation multiplier=2.0
2. [x] **ATR**: period=14, multiplier for stop loss
3. [x] **ADX regime filter**: < 25 = ranging (trade), 25-30 = caution, > 30 = trending (skip)
4. [x] **Entry signal**: price at lower BB touch + RSI < 40 + ranging regime
5. [x] **Exit signal**: price at upper BB + RSI > 60 OR ATR trailing stop
6. [x] **Cost-adjusted widths**: ETH/cbBTC 1.5-3%, AERO 2-5%, ZEN 3-6%, VFY 6-12%

### Steps
1. [x] Write `REFERENCES/range-engine-spec.md` — exact formulas, parameters, pseudocode ✅
2. [x] Define per-pair parameter sets (BB period, ATR period, min width, fee rate) ✅
3. [x] Design entry/exit signal logic with regime gating ✅
4. [ ] Commit spec → just did ✅

---

## Phase 3 — Implement Range Engine ✅ COMPLETE

**Goal:** Build the new engine alongside existing code (no replace until tested).

### Steps
1. [x] Snapshot existing `support_resistance.py` to `support_resistance_v1.py` (preserve old logic)
2. [x] Create `range_engine.py`:
      - `calculate_bb_sr()` — Bollinger Bands S/R
      - `calculate_atr()` — ATR calculation
      - `calculate_adx()` — ADX regime filter
      - `get_range()` — hybrid orchestrator returning S/R, regime, quality score
      - `calculate_min_width()` — cost-adjusted floor per pair
      - `get_entry_exit()` — entry/exit/target/stop per current regime
3. [x] Create `range_engine_config.py` — per-pair parameter sets
4. [x] Create `test_range_engine.py` — unit tests for all functions
5. [x] Commit: `feat: range engine v2 core`

---

## Phase 4 — Multi-Pair Scanner Integration ✅ COMPLETE

**Goal:** Extend the existing scanner to use the new engine across 3-5 pairs.

### Steps
1. [x] Audit `range_scanner.py` — understand current pair scanning logic
2. [x] Add new pairs to scanner config: WETH/USDC, AERO/USDC, ZEN/WETH, cbBTC/WETH, VFY/USDC
3. [x] Wire new `range_engine.py` into scanner — parallel run with v1, log both
4. [x] Add liquidity filter: skip pair if TVL < threshold (per-pair thresholds)
5. [x] Commit: `feat: multi-pair range scanner`

---

## Phase 5 — Backtesting Framework 🔲 PENDING

**Goal:** Prove the engine works before touching real money.

### Steps
1. [ ] Design backtester: replay historical candles, apply range engine, simulate entry/exit with realistic costs
2. [ ] Pull historical data for each pair (GeckoTerminal or subgraph)
3. [ ] Run backtest: 30+ days, multiple pairs, different market regimes
4. [ ] Metrics: win rate, avg profit, max drawdown, slippage realized vs expected
5. [ ] Tune parameters per pair based on results
6. [ ] Commit: `feat: range engine backtest results`

### Success Criteria
- Win rate ≥ 55% after costs across backtest period
- No pair shows consistently negative expectancy
- Slippage modeled accurately (within 0.5% of simulated)

---

## Phase 6 — Pipeline Integration & Dry-Run 🔲 PENDING

**Goal:** Integrate with `run_range_pipeline.py`, verify no TX would fire in dry-run.

### Steps
1. [ ] Wire `range_engine.py` output into pipeline signal flow
2. [ ] Add circuit breaker: skip pair if regime = trending (ADX > 30)
3. [ ] Verify `range_quality_score()` gates entry (reject low-quality ranges)
4. [ ] Run full pipeline dry-run — no live TX, confirm circuit breakers fire
5. [ ] Commit: `feat: range engine pipeline integration`

---

## Phase 7 — Closeout 🔲 PENDING

### Steps
7A. [ ] Lessons: what worked in backtest, what parameter drift to watch
7B. [ ] Update `support_resistance.py` docstring to point to `range_engine.py`
7C. [ ] Update PROJ-range-trader-v2.md: mark range calculation overhaul complete
7D. [ ] Update memory with final config
7E. [ ] Archive: move doc to `tasks/completed/2026-04/`
7F. [ ] Final commit: `complete: range-calculator-v2`

---

## Key Risks

| Risk | Mitigation |
|------|------------|
| BB parameters overfit to backtest | Use conservative defaults; validate on multiple timeframes |
| Thin pairs (VFY) slippage worse than modeled | Use 2x slippage buffer for VFY; start with deeper pairs in competition |
| ADX lag in fast market moves | Add price momentum filter alongside ADX |
| Data source rate limits | Implement retry logic (already exists from Phase 3) |
| Multiple pairs = more complex state management | Keep positions.json schema unchanged; only add pair_id to entries |

---

## Git Workflow

| When | Action |
|------|--------|
| Plan doc created | `git add -A && git commit -m "plan: range-calculator-v2"` |
| Phase 3 complete | `git add -A && git commit -m "feat: range engine v2 core"` |
| Phase 4 complete | `git add -A && git commit -m "feat: multi-pair range scanner"` |
| Phase 5 complete | `git add -A && git commit -m "feat: range engine backtest results"` |
| Phase 6 complete | `git add -A && git commit -m "feat: range engine pipeline integration"` |
| Archive | `git add -A && git commit -m "complete: range-calculator-v2"` |

---

**Status:** Phase 2 ✅ COMPLETE — Phase 3 🔲 ON DECK — awaiting CoinGecko API key to begin implementation
