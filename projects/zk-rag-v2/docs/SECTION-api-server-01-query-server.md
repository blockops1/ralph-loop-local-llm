# SECTION-api-server-01: RAG Query API Server

**Parent:** PROJ.md
**Status:** In Discussion — decisions made, PRD being finalized
**Date:** 2026-04-16

This section covers the design, implementation, and deployment of the RAG Query API Server.

---

## Current Status

Decisions made on all open items (see below). PRD being finalized before Ralph JSON PRD is written.

**PRD:** `docs/PRDs/PRD-api-server-01-query-server.md`
**PRD Status:** In Discussion — feature set finalized, decisions recorded

---

## Resolved Decisions

All open items resolved 2026-04-16.

| Item | Decision | Rationale |
|------|----------|-----------|
| OpenResty proxy (local dev) | Bypass OpenResty; API server binds directly to `127.0.0.1:8100` | Local dev machine has no OpenResty. Clients hit `http://10.120.60.102:8100` directly over HTTP. Production adds OpenResty + HTTPS + auth at the VPS layer. |
| Deployment method | systemd unit | Available on both machines, straightforward logging via `journalctl`, restart-on-failure, no extra tooling |
| CORS | None — handle at proxy layer if needed | API server sits behind OpenResty; no browser-based local dev access anticipated |
| BM25 index build | Inside Pipeline G batch, incremental pickle | Pipeline G already has all chunk text loaded; natural place to build index without a separate process |
| Pipeline G metadata enrichment | Add `category`, `doc_type`, `source`, `file_size_bytes` to Qdrant payload | Useful for filtering and display; all fields already exist in registry, just need to add to `build_payload()` |

### BM25 Implementation Details
- Library: `rank_bm25`
- Index file: `/data/military-documents/bm25_index.pkl`
- Build at end of each Pipeline G batch run
- Incremental: load existing index → append new docs → re-pickle (do not rebuild from scratch)

### Pipeline G Payload Enrichment
Add these four fields to `build_payload()` in `pipeline_g.py`:

| Field | Source in registry |
|-------|-------------------|
| `category` | `entry.get("category", "")` |
| `doc_type` | `entry.get("doc_type", "")` |
| `source` | `entry.get("source", "")` |
| `file_size_bytes` | `entry.get("file_size_bytes")` |

---

## Architecture Principle

The pipeline and the API server are completely decoupled. Pipelines A-G are offline batch processes that write to Qdrant and exit. The API server is a long-running service that only reads from Qdrant and serves queries. The only shared state is Qdrant itself.

```
[User/Agent]
    ↓ HTTPS
[OpenResty]  ← website + API gateway (rate limit, geo-filter, auth) — production only
    ↓ proxy_pass
[API Server]  ← lightweight service: query Qdrant, return results
    ↓
[Qdrant]     ← vector data (produced by pipeline)
```

**Local dev:** API server binds directly to `127.0.0.1:8100`, no OpenResty in path.
**VPS (production):** OpenResty handles HTTPS + auth, proxies to API server.

---

## Phases

### Phase 1 — Local Dev, Query/Response Working

**Goal:** Get the API server running locally, prove query/response works, then deploy to VPS.

**Steps:**
- [ ] Finalize PRD (this document)
- [ ] Write Ralph JSON PRD from finalized PRD
- [ ] Implement API server — FastAPI + uvicorn, bind `127.0.0.1:8100`
- [ ] Add `category`, `doc_type`, `source`, `file_size_bytes` to Pipeline G `build_payload()` — PR or inline fix
- [ ] Implement BM25 index build inside Pipeline G batch (incremental pickle)
- [ ] Run Pipeline G with `--batch` to verify new metadata fields + BM25 build
- [ ] Test queries locally: semantic search, per-branch filtering, hybrid search, metadata returns
- [ ] Verify feature parity with existing VPS RAG API
- [ ] Write systemd unit for API server (`zk-rag-api.service`)
- [ ] Install and enable on local dev machine (DeRuyter)
- [ ] Verify service stays up under load
- [ ] Copy working system to VPS
- [ ] Update DNS / SSL / OpenResty config on VPS
- [ ] Cut over production traffic

---

### Phase 2 — ZK Proof of Provenance

**Goal:** Each query response includes cryptographic proof that the returned information was part of an on-chain registered corpus.

**Steps:**
- [ ] On query, retrieve Merkle proof paths for matched chunks
- [ ] Include `merkle_root`, `merkle_leaf_hash`, `merkle_path`, `merkle_tree_depth` in response
- [ ] Include EVM tx hash + block explorer link for the Merkle root emission (Horizen testnet explorer)
- [ ] Generate Plonky2 ZK proof proving the query processor correctly retrieved chunks from the Merkle tree
- [ ] Submit proof to zkverify
- [ ] Include zkverify explorer link in response
- [ ] Response format includes: chunk data, Merkle proof, EVM tx link, zkverify proof link

---

### Phase 3 — X402 Payment for Information

**Goal:** Charge for API access using X402 (Stripe's HTTP payment protocol).

**Steps:**
- [ ] Add X402 payment header handling to OpenResty
- [ ] Configure payment-gated endpoints
- [ ] Link subscription/credits to API key or wallet
- [ ] Free tier: limited queries/day. Paid tier: unlimited.
