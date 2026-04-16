# PRD-api-server-01: RAG Query API Server

**Status:** Approved
**Author:** Fred (data backbone)
**Date:** 2026-04-16
**Depends On:** Pipeline G (Qdrant upsert) producing data in Qdrant

---

## 1. Problem Statement

The RAG system has pipelines (A→B→C→D→E→F→G) that process PDFs into searchable vector chunks stored in Qdrant. The pipelines are purely data generation — they do not serve data to users or agents.

There is currently no way to query the Qdrant data from the outside world.

The query API server provides a clean interface: receives a search query → embeds it → searches Qdrant → returns results. It has no pipeline dependencies and is not involved in data generation.

---

## 2. Goals

- Provide a REST API for semantic search against Qdrant
- Accept natural-language queries and return relevant document chunks
- Return results with full chunk text + all metadata
- Serve images from filesystem for documents
- Health check and API discovery endpoints for monitoring and client tooling
- Thin layer: query → embed → Qdrant → return. No PDF processing, no pipeline logic

---

## 3. Architecture

```
Local dev:
[Client] → HTTP → [API Server :8100] → [Qdrant /data/qdrant/database]

Production (VPS):
[Client] → HTTPS → [OpenResty] → [API Server :8100] → [Qdrant /data/qdrant/database]
```

**No PDF processing. No pipeline code. Pure query interface.**

Local dev: API server binds directly to `127.0.0.1:8100`, no OpenResty in the path.
Production: OpenResty handles HTTPS + Bearer token auth, proxies to API server.

---

## 4. Query Flow

1. Receive `POST /api/query` with JSON body `{"query": "...", "top_k": 5, "collection": "army", "hybrid": true}`
2. Load embedding model lazily on first request (Qwen/Qwen3-Embedding-0.6B, 1024-dim)
3. Embed query text → vector
4. Search Qdrant: if `hybrid: true`, merge 0.5 × vector cosine score + 0.5 × normalized BM25 score; if `false`, vector-only
5. Return matching chunks with full payload (text + all metadata from Qdrant)

---

## 5. API Endpoints

### 5.1 Query (search)

```
POST /api/query
Authorization: Bearer ***
Content-Type: application/json

{
  "query": "enemy prisoner of war handling procedures",
  "top_k": 5,
  "collection": "army",
  "hybrid": true
}
```

Response:
```json
{
  "results": [
    {
      "doc_id": "0a21e769...",
      "chunk_id": "0a21e769...-0",
      "title": "Army FM 19-10",
      "branch": "army",
      "category": "Ground Tactics & Small Unit Operations",
      "page": 2,
      "section": "3-4",
      "section_title": "EPW Handling",
      "text": "Full text of the matching chunk...",
      "score": 0.847,
      "merkle_root": ["0x...", "0x..."],
      "merkle_leaf_hash": "0x...",
      "merkle_path": [{"hash": "0x...", "at_depth": 3}],
      "merkle_tree_depth": "9",
      "evm_tx_hash": "0x..."
    }
  ],
  "query": "enemy prisoner of war handling procedures",
  "collection": "army",
  "total": 1
}
```

**Collection parameter:** `army`, `navy`, `marines`, `coast_guard`, `air_force`, `other`, or `*` (searches all collections and merges results).

**Hybrid parameter:** `true` for 0.5 × vector + 0.5 × BM25 scoring; `false` for vector-only. Default `true`.

**Auth:** OpenResty enforces Bearer token auth. API server receives `X-Key-Tier: privileged` or `X-Key-Tier: limited` header.

### 5.2 Collections (list)

```
GET /api/collections
```

Response:
```json
[
  {
    "name": "army",
    "vector_count": 12345,
    "vector_dim": 1024,
    "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
    "doc_ids": ["doc1", "doc2"],
    "chunk_count": 12345
  }
]
```

No auth required.

### 5.3 Collection info

```
GET /api/collections/{collection}
```

Response:
```json
{
  "name": "army",
  "vector_count": 12345,
  "vector_dim": 1024,
  "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
  "doc_ids": ["doc1", "doc2"],
  "chunk_count": 12345
}
```

No auth required.

### 5.4 Document metadata

```
GET /api/doc/{doc_id}
```

Returns document metadata from Qdrant payload (all fields for that doc's chunks, deduplicated). No auth required.

### 5.5 Chunk context (window)

```
GET /api/context?doc_id=X&chunk_index=N&collection=Y&window=1
```

Return a window of chunks from a single document by position. `window=1` returns chunk_index N-1, N, N+1. No auth required.

### 5.6 Images (filesystem serving)

```
GET /images/{doc_id}/{filename}
```

Serve document images from filesystem. No auth required.
Images live at `/data/military-documents/images/{doc_id}/`.

### 5.7 Image manifest

```
GET /api/images/{doc_id}/{page_num}
```

Return list of images for a specific page of a document. No auth required.

### 5.8 API manifest

```
GET /api/manifest
```

Machine-readable API description. No auth required.

### 5.9 OpenAPI spec

```
GET /api/openapi.json
```

OpenAPI 3.1 spec for client tooling. No auth required.

### 5.10 Health

```
GET /health
```

Response:
```json
{
  "status": "ok",
  "qdrant": "connected",
  "model": "Qwen/Qwen3-Embedding-0.6B",
  "bm25": "loaded"
}
```

### 5.11 Query stats

```
GET /query_stats
```

Query volume statistics. No auth required.

---

## 6. Data Storage

- **Qdrant path:** `/data/qdrant/database`
- **Collection per branch:** `army`, `navy`, `marines`, `coast_guard`, `air_force`, `other`
- **Vector dimension:** 1024 (Qwen/Qwen3-Embedding-0.6B)
- **Distance metric:** Cosine
- **Payload metadata:** All document metadata stored in Qdrant payloads at ingest time via Pipeline G. No registry file lookup at query time.
- **BM25 index:** `/data/military-documents/bm25_index.pkl` — built incrementally at end of each Pipeline G batch run using `rank_bm25`. If missing, API falls back to vector-only search with a warning log.
- **Images:** Filesystem at `/data/military-documents/images/{doc_id}/`

---

## 7. Configuration

| Parameter | Value |
|-----------|-------|
| Qdrant path | `/data/qdrant/database` |
| Qdrant mode | Local (path-based, NOT HTTP client) |
| Embedding model | `Qwen/Qwen3-Embedding-0.6B` (1024-dim) |
| Embedding tool | `sentence-transformers` on CPU |
| Default `top_k` | 5 |
| Max `top_k` | 50 |
| BM25 index path | `/data/military-documents/bm25_index.pkl` |
| Images directory | `/data/military-documents/images/` |
| API bind | `127.0.0.1:8100` |

---

## 8. Design Decisions

### 8.1 Hybrid Search
Default: `hybrid: true` — 0.5 × vector cosine score + 0.5 × normalized BM25 score, merged and sorted by combined score.

BM25 index loaded at startup from `/data/military-documents/bm25_index.pkl`. If the index file is missing, fall back to vector-only search and log a warning at startup.

### 8.2 Lazy Model Initialization
Embedding model loaded on first query request, not at startup. Keeps cold-start memory small and startup time fast.

### 8.3 Authentication
Handled by OpenResty at the VPS layer, not the API server. OpenResty enforces Bearer token auth with two tiers:
- **Privileged:** 40 req/min (key: `VcZ4hg+IZcQPhcG3ta+b4ICHxDF+NaEaa+9EHWsN1qU=`)
- **Limited:** 5 req/min (key: `Bp18OLgIfbUXOVXsrpZEbpwozgsnk7ANIugm9XTXMik`)

API server receives `X-Key-Tier` header from OpenResty. Authenticated endpoints: `/api/query`. Public endpoints: all others.

### 8.4 Full Text in Response
Qdrant payload stores full chunk text. API returns full text in response, not a 200-char preview.

### 8.5 Metadata in Qdrant Payload
All document metadata (title, branch, category, doc_type, source, pub_year, file_size_bytes, ia_identifier, merkle fields, evm fields) stored in Qdrant payload at ingest time via Pipeline G. No separate registry file lookup at query time.

### 8.6 Local Dev: No OpenResty
During local development on DeRuyter (10.120.60.102), there is no OpenResty. The API server binds directly to `127.0.0.1:8100` and accepts plain HTTP. Auth headers are not enforced locally.

### 8.7 Deployment: systemd
The API server runs as a systemd unit (`zk-rag-api.service`). No Docker or other containerization.

---

## 9. Implementation Notes

- **Framework:** FastAPI + uvicorn
- **Qdrant client:** Local path mode (`QdrantClient(path="/data/qdrant/database")`)
- **Embedding:** `sentence-transformers` Python library (`Qwen/Qwen3-Embedding-0.6B`)
- **BM25:** `rank_bm25` library; BM25Okapi model saved/loaded from pickle
- **Image serving:** FastAPI static file mount at `/images/` → `/data/military-documents/images/`
- **Lazy init:** Embed model and Qdrant client initialized on first request (not at startup)
- **Auth:** OpenResty handles Bearer token; API server receives `X-Key-Tier` header
- **API bind:** `127.0.0.1:8100`

---

## 10. Resolved Decisions

| Question | Decision | Date |
|----------|----------|------|
| Vector dimension | 1024 (Qwen3-Embedding-0.6B) | 2026-04-16 |
| OpenResty proxy (local dev) | Bypass; direct HTTP to `:8100` | 2026-04-16 |
| Deployment method | systemd unit | 2026-04-16 |
| CORS | None | 2026-04-16 |
| BM25 index build | Inside Pipeline G batch, incremental pickle | 2026-04-16 |
| Pipeline G metadata | Add category, doc_type, source, file_size_bytes to Qdrant payload | 2026-04-16 |
