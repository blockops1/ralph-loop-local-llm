# Code Review: solana-trader-core
**Date:** 2025-01-10
**Reviewer:** Ralph Review Agent
**Files reviewed:** 8
**Stories in PRD:** 9
**Overall:** ⚠️ Fix first

---

## Summary
The Solana SM trader implements a paper-trade-first architecture mirroring base-trader, with 6 concurrent positions max, $50 fixed per trade, 20% stop-loss, and tiered exits (+50% sell half, +100% sell 75% total, trailing 25%). The implementation is functionally complete but has several spec deviations: it fetches directly from Nansen API instead of reading a shared cache written by base-trader, uses different field names than specified, and has a BTC kill switch timing mismatch (5h vs 4h). These are architectural deviations that work but differ from the PRD's intended design.

---

## 🔴 Critical (must fix before ship)
| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | scanner.py | Missing `SHARED_CACHE` read — PRD specifies reading from `shared/nansen_cache.json` written by base-trader, but implementation directly calls Nansen API. This breaks the intended architecture where base-trader writes cache and solana-trader reads it. | Architectural break — solana-trader no longer depends on base-trader running first, but this was intentional per PRD. | Either: (a) Add shared cache read as primary, API as fallback; or (b) Document this as intentional deviation. |
| 2 | scanner.py | Function named `get_btc_5h_change()` but PRD specifies `get_btc_4h_change()`. The 5h window (last 5 hourly data points) differs from the 4h kill switch intent. | BTC kill switch triggers at wrong time window — could miss a 4h crash or trigger falsely on 5h data. | Rename to `get_btc_4h_change()` and fetch exactly 4 hourly data points (indices -4 to -1). |
| 3 | signal_filter.py | Uses `trader_count` and `net_flow_24h_usd` from flow_intel, but PRD specifies `smart_trader_wallet_count` and `smart_trader_net_flow_usd`. Field names don't match the Nansen flow-intelligence endpoint schema. | Scoring uses wrong fields — may score incorrectly if Nansen returns different field names. | Update to use correct Nansen field names: `smart_trader_wallet_count`, `smart_trader_net_flow_usd`, `whale_net_flow_usd`, `top_pnl_net_flow_usd`. |
| 4 | run_pipeline.py | Missing `PAPER_TRADE` environment variable check — PRD specifies `PAPER_TRADE = os.environ.get('PAPER_TRADE', 'true').lower() != 'false'` but implementation only reads from config. | Cannot override paper trade mode via env var — must edit config.yaml. | Add `paper = config.get('paper_trade', True) or os.environ.get('PAPER_TRADE', 'true').lower() != 'false'` in main(). |

---

## ⚠️ Medium (fix before next sprint)
| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | scanner.py | `load_candidate_tokens()` returns `netflow_24h_usd` but PRD specifies `netflow_usd`. Downstream code may break if expecting `netflow_usd`. | Rename field to `netflow_usd` to match PRD spec. |
| 2 | signal_filter.py | Hard disqualifier checks `smart_trader_wallets == 0` but PRD comment says "Zero smart trader wallets — no SM signal". The field should be `smart_trader_wallet_count` per PRD. | Rename variable to `smart_trader_wallet_count` for clarity. |
| 3 | execute.py | Missing module-level `PAPER_TRADE` flag — PRD specifies `PAPER_TRADE = os.environ.get('PAPER_TRADE', 'true').lower() != 'false'` at module level. | Add module-level flag and use it as default in `buy_token()` and `sell_token()`. |
| 4 | exit_monitor.py | `check_exits()` has `paper_trade: bool = True` default but PRD specifies checking config. Should read from config or env. | Change default to read from `os.environ.get('PAPER_TRADE', 'true').lower() != 'false'`. |
| 5 | run_pipeline.py | `_startup_key_check()` alerts to trading group only (`-5264050975`) but PRD doesn't specify this. May miss critical alerts. | Consider alerting to both trading group and admin channel for startup failures. |
| 6 | scanner.py | `_enrich_liquidity()` uses batch DexScreener request but doesn't handle rate limits or pagination for >50 tokens. | Add rate limit handling and pagination for large candidate sets. |

---

## 🔵 Minor (nice to have)
| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | scanner.py | `DISCOVERY_CACHE_TTL = 3600` (1 hour) but PRD comment says "4 hours". | Either change TTL to 14400 or update comment. |
| 2 | signal_filter.py | Pillar 2 scoring uses `whale_nf` and `pnl_nf` but doesn't check if they exist before accessing. | Add `.get()` with defaults for robustness. |
| 3 | decision.py | `format_decision()` doesn't include `flow_intel` in output for debugging. | Add flow_intel summary to decision dict for traceability. |
| 4 | run_pipeline.py | `notify()` function has hardcoded group_id but PRD doesn't specify this. | Move to config.yaml for flexibility. |
| 5 | execute.py | `_TOKEN_DECIMALS` hardcoded with only USDC — should include common Solana tokens. | Add WSOL, USDT, and other common tokens to cache. |

---

## ✅ Verified Working
- **Config structure**: `config.yaml` correctly implements all PRD-specified settings (max_positions=6, stop_loss_pct=0.20, tiered exits, entry_score_threshold=175)
- **Directory structure**: `config/`, `data/`, `scripts/` directories created as specified
- **Position management**: `positions.py` correctly implements load, save, add, remove, get operations
- **Tiered exit logic**: `exit_monitor.py` correctly implements Tier 1 (+50% sell 50%), Tier 2 (+100% sell 25% more), trailing stop (30% from peak), breakeven stop, and hard stop loss
- **Blacklist and cooldown**: `decision.py` correctly implements blacklist loading and 4-hour cooldown for failed buys
- **Telegram notifications**: `run_pipeline.py` correctly sends PRE-BUY and BUY alerts
- **Credit tracking**: `run_pipeline.py` correctly logs Nansen credit usage to `credit_log.jsonl`
- **Signal logging**: `run_pipeline.py` correctly logs all scored signals to `signal_log.jsonl`
- **Run logging**: `run_pipeline.py` correctly logs run summary to `run_log.jsonl`
- **Paper trade mode**: `execute.py` correctly bypasses real execution when `paper_trade=True`
- **Jupiter Ultra API integration**: `execute.py` correctly implements quote → sign → execute flow
- **Token balance queries**: `execute.py` correctly implements `get_sol_balance()` and `get_token_balance()` via Helius RPC

---

## Reviewer Notes
- **Architecture deviation**: The PRD intended for solana-trader to read a shared Nansen cache written by base-trader, creating a dependency. The implementation instead fetches directly from Nansen API, making it independent. This is functionally better (no dependency) but differs from spec.

- **Field name inconsistencies**: Multiple files use different field names than PRD (`trader_count` vs `smart_trader_wallet_count`, `netflow_24h_usd` vs `netflow_usd`). This suggests the implementation was adapted to actual Nansen API responses rather than following the PRD's idealized schema.

- **BTC kill switch timing**: The 5h vs 4h discrepancy is minor but worth fixing for consistency. The PRD says "4h change" but implementation uses 5 hourly data points.

- **Test coverage**: No unit tests exist. The PRD only specifies smoke test acceptance criteria. Consider adding tests for `score_signal()` edge cases and `check_exits()` tier transitions.

- **Error handling**: Generally adequate but some exceptions are swallowed silently (e.g., `scanner.py` `_enrich_liquidity()` catches all exceptions and prints but doesn't retry or escalate).

- **Logging**: Good use of structured logging (JSONL files) but no log rotation or size limits — `signal_log.jsonl` could grow unbounded.

- **Security**: Private key handling via env vars is correct. No secrets in code.

- **Performance**: Discovery caching (1 hour TTL) is a good optimization. Flow intelligence caching (30 min) is also reasonable.

- **The `get_btc_5h_change()` function name is misleading** — it fetches the last 5 hourly data points which represents ~4 hours of change (t-4h to t), not 5 hours. This is actually correct for a "4h change" but the naming is confusing.

