# PRD-03: RAG Embedding and Retrieval Integration

## 1. Background and Context

ZK-RAG proof generation must be tightly coupled with the actual RAG retrieval pipeline. When a user query arrives, the system retrieves the top-K most relevant chunks from the vector database, includes them in the LLM's context window, and simultaneously prepares the ZK witness data (Merkle proofs) for those chunks.

This PRD covers the integration between the existing Qdrant-based vector search infrastructure and the Merkle corpus built in PRD-01, producing a unified retrieval interface that returns both the chunks and their cryptographic proof data.

## 2. Goals and User Stories

**As a** ZK-RAG prover,
**I want** a retrieval function that returns top-K chunks along with their Merkle proofs
**so that** I can feed both the chunks to the LLM and the proof data to the ZK circuit.

**As a** system,
**I want** the retrieval index and the Merkle corpus to stay synchronized
**so that** a proof verified today remains valid against the same corpus.

## 3. Functional Requirements

### FR-1: Unified Retrieval API

```rust
pub struct RetrievedChunk {
    pub chunk_id: String,           // e.g., "doc123_0042"
    pub text: String,              // Raw text content
    pub sorted_index: usize,        // Position in sorted Merkle tree
    pub merkle_proof: MerkleProof, // Sibling hashes from PRD-01
    pub score: f32,                // Vector similarity score
}

pub async fn retrieve_chunks_with_proofs(
    query: &str,
    k: usize,
    corpus_store: &CorpusMerkleStore,
) -> Result<Vec<RetrievedChunk>, Error>;
```

### FR-2: Qdrant Integration
- Use existing Qdrant client (already deployed; connection details via `QDRANT_URL` in `.env`)
- Query the existing collection (collection name TBD; likely `military_docs`; set via `QDRANT_COLLECTION` in `.env`)
- Return top-K results by cosine similarity
- Map Qdrant payload `chunk_id` → filesystem chunk file → load text
- Chunk text path: `/data/rag/chunks/{doc_id}_{chunk_index}.txt` (configurable via `CHUNKS_DIR` in `.env`)

### FR-3: Index Synchronization
- The Qdrant index and the Merkle tree must reference the same chunk set
- When the corpus is rebuilt (PRD-01 FR-6), Qdrant must also be updated
- Chunk IDs must be consistent between Qdrant payloads and Merkle tree indices
- API: `sync_corpus(chunks_dir, qdrant_collection)` — rebuilds both Merkle tree and Qdrant index

### FR-4: Chunk Text Loading
- Load chunk text from `{CHUNKS_DIR}/{doc_id}_{chunk_index}.txt` (env var — never hardcode)
- Compute `PoseidonHash(chunk_text)` and confirm it matches the leaf hash at `sorted_index` in the Merkle tree
- If hash mismatch: log error, exclude chunk from results, do not fail silently

### FR-5: Fallback Behavior
- If Qdrant is unavailable: return error (do not fall back to naive search)
- If chunk file is missing: log warning, skip chunk, return K-1 results if possible
- If fewer than K chunks available: return whatever is available (K' < K)

## 4. Data Flow

```
Query string
     |
     v
[Qdrant vector search] --> [Top-K chunk_ids + scores]
     |
     v
[Load chunk text from filesystem] --> [RetrievedChunk { text, score }]
     |
     v
[Look up sorted_index + MerkleProof from CorpusMerkleStore] --> [RetrievedChunk { ... + proof }]
     |
     v
[Verify: PoseidonHash(chunk_text) == tree.leaf[sorted_index]]
     |
     v
Return Vec<RetrievedChunk> to caller (LLM + ZK circuit)
```

## 5. Edge Cases

| Scenario | Handling |
|----------|----------|
| Qdrant collection missing | Return error with collection name hint |
| Chunk file missing from disk | Skip; log warning; return partial results |
| Hash mismatch (corruption) | Skip chunk; log CRITICAL error; alert |
| Query returns < K chunks | Return available chunks; proceed with K' < K |
| Empty query | Return error; no meaningful retrieval possible |

## 6. Environment and Portability

See **Section 4 (Portability and System Setup)** in the project plan for the full `.env` schema and directory layout.

| Variable | Required | Description |
|----------|----------|-------------|
| `CHUNKS_DIR` | Yes | Root directory containing chunk files |
| `QDRANT_URL` | Yes | Qdrant server URL |
| `QDRANT_COLLECTION` | Yes | Qdrant collection name |

## 7. Acceptance Criteria

- [ ] `retrieve_chunks_with_proofs("...", k=5, &store)` returns exactly 5 `RetrievedChunk` objects (or fewer if corpus < 5)
- [ ] Each `RetrievedChunk.merkle_proof` verifies against `store.cap` when passed to plonky2 verifier
- [ ] Each `RetrievedChunk.text` hashes to the corresponding leaf in the Merkle tree
- [ ] The Qdrant collection and Merkle tree contain the same set of chunks (verified by chunk_id set equality)
- [ ] Corpus rebuilds are idempotent: rebuilding does not break existing proofs against old root
- [ ] Unit tests: mock Qdrant responses; test with 3, 5, and 0 chunks returned
- [ ] Integration test: run real query against real Qdrant + real corpus store (reads `QDRANT_COLLECTION` and `CHUNKS_DIR` from environment)
