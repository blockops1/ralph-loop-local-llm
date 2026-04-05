# ZK-RAG: Zero-Knowledge Retrieval-Augmented Generation
## Project Plan v1.0

---

## 1. Project Overview

### Purpose
Build a ZK-RAG system that proves—in zero-knowledge—that a language model was grounded in an authentic, untampered document set when generating a response. A verifier can check the proof without seeing the documents, the query, or the model weights.

### Core Insight
A Merkle tree root commits to the document corpus. A ZK proof demonstrates that the retrieved context chunks used in LLM generation are genuine members of that committed set—without revealing which documents, which chunks, or the query itself.

### Threat Model
- **Honest Prover**: Runs the full RAG pipeline and generates an honest proof
- **Malicious Prover**: Cannot forge proof of a document that was not in the committed corpus
- **Verifier**: Checks proof validity; learns nothing beyond "proof is valid"

---

## 2. Architecture

### 2.1 High-Level Data Flow

```
[Document Corpus]
       |
       v
[Chunk + Embed  ]  -->  [Merkle Tree Build]
       |                      |
       |                      v (MerkleCap root = 16 Poseidon hashes)
       |               [Publish root to Horizen EVM contract]
       |               (plain hash data; no ZK proof at this step)
       |
       v
[Query]
       |
       v
[Vector Search]  -->  [Top-K Chunks]
       |
       v
[LLM Generation]  (uses retrieved chunks as context)
       |
       v
[ZK Proof Generation]  (plonky2 STARK)
  - Prove each top-K chunk is in Merkle tree with this root
  - Prove LLM input includes those chunks
  - Output: (proof, public_inputs)
       |
       v
[Kurier / zkVerify]  (on-chain proof verification)
```

### 2.2 Merkle Tree Construction

**Leaf = PoseidonHash(chunk_text_bytes)**
- Each chunk's raw bytes are processed as field elements and hashed with Poseidon
- Chunks are sorted before building the tree (deterministic ordering by leaf hash)
- Tree uses Poseidon permutation internally (plonky2-compatible)

**Root**
- MerkleCap of height 4 → root consists of 16 Poseidon hash values
- The root (as 16 field elements) is the primary public input to the ZK proof

### 2.3 Circuit Design (plonky2 v0.2.2)

**Primary Circuit: `zk_rag_circuit`**

Public Inputs (PI):
1. `cap[16]` — MerkleCap (height 4) — the corpus root commitment
2. `output_hash` — Poseidon hash of the full LLM response text

Private Inputs (witness):
1. `chunk_data[i]` — bytes of the i-th retrieved chunk
2. `chunk_hash[i]` — Poseidon digest of chunk_data[i]
3. `merkle_proof[i]` — sibling digest path for chunk i
4. `chunk_index[i]` — integer index of chunk i in the Merkle tree

Constraints enforced in circuit:
- For each chunk i: `verify_merkle_proof_to_cap(chunk_hash[i], index_bits[i], cap, proof[i])`
- PoseidonHash(chunk_data[i]) == chunk_hash[i] (re-hash to confirm integrity)
- PoseidonHash(llm_full_input) == output_hash

**Note on text in plonky2 circuits**: plonky2 field elements are ~64-bit (Goldilocks). Text bytes are split into 64-bit limbs and fed to Poseidon. The circuit enforces that recomputed Poseidon matches the claimed leaf hash. No actual string comparison is needed inside the circuit.

### 2.4 Multi-chunk Proof (K=5)

- Up to K=5 retrieved chunks per query are supported
- Each chunk has its own Merkle proof path verified in the same circuit
- The set of chunk indices [i1..iK] is part of the public input (acceptable to reveal)
- LLM input hash commits to the full prompt (chunks + query), not to individual chunks

### 2.5 On-Chain Architecture: Two-Tier Model

ZK-RAG uses **two separate on-chain layers** for two separate jobs:

**Layer 1 — Commitment (Horizen EVM, at ingestion):**
- Merkle root = 16 Poseidon field elements = a small (~200 byte) commitment
- Published once per corpus snapshot to a `CorpusRegistry` contract on Horizen EVM (ZEND)
- The root is plain data — no ZK proof involved, just a `push` to a smart contract
- As corpus grows: append new roots; old proofs remain valid against old roots
- This is the "I commit to this document set" signal

**Layer 2 — Proof Verification (zkVerify / Kurier, at query time):**
- plonky2 STARK proof generated at query time (fastest to prove at query)
- Proof sent to Kurier → verified on zkVerify smart contract
- Verifier checks: "these K chunks exist in the Merkle tree with this root"
- The Merkle root was already published, so the proof only needs to prove membership

**Why this split:**
- Publishing a Merkle root on Horizen EVM is cheap (plain calldata)
- Generating a ZK proof on Horizen EVM would be prohibitively slow/expensive
- Kurier/zkVerify is built exactly for fast on-chain STARK verification
- Two chains, two jobs — each optimized for its task

---

## 3. Technology Stack

| Component | Technology | Version | Notes |
|-----------|-----------|---------|-------|
| ZK Framework | plonky2 | 0.2.2 | Stark proof; Poseidon hash |
| Proof Aggregation | Recursion | plonky2 recursion | Nest proofs to compress final proof |
| Proof Verification On-chain | Kurier / zkVerify | — | On-chain STARK verification |
| Commitment Storage | Horizen EVM (ZEND) | — | Merkle root published here at ingestion |
| Cloud Proving | Local | CLI | CPU-based plonky2 proving for development |
| Off-chain Verifier | kurier.xyz | API | Lightweight verification endpoint |
| Rust Tooling | cargo, rustc | stable | Local dev and testing |
| Embedding Model | sentence-transformers | — | Local CPU embedder for corpus |
| Vector DB | Qdrant | — | Already deployed; hosts corpus embeddings |
| LLM | Local LLM via Ollama | — | Grounded generation |

### 3.2 Kurier API Integration

Lightweight proof verification service.

```bash
curl -X POST https://api.kurier.xyz/verify \
  -H "Content-Type: application/json" \
  -d '{"proof": {...}, "public_inputs": [...]}'
```

**OPEN QUESTION**: Kurier base URL and exact endpoint format need confirmation from `docs.kurier.xyz`. The structure above is illustrative.

---

## 4. Portability and System Setup

### 4.1 Environment Configuration

All system-specific values are driven by environment variables. Create a `.env` file in the project root:

```bash
# Required
CHUNKS_DIR=/data/rag/chunks

# Qdrant (PRD-03, PRD-04)
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=military_docs

# Ollama LLM (PRD-04)
OLLAMA_MODEL=llama3
OLLAMA_BASE_URL=http://localhost:11434

# Kurier verification (PRD-06) — TODO: confirm base URL from docs.kurier.xyz
KURIER_BASE_URL=https://api.kurier.xyz
KURIER_API_KEY=        # optional; leave empty for local-only verification
KURIER_CIRCUIT_ID=zk-rag-v1

# ZK Circuit (PRD-02)
ZK_MAX_CHUNK_LIMBS=8192       # max field limbs per chunk text
ZK_MAX_LLM_INPUT_LIMBS=16384  # max field limbs for full prompt
ZK_MAX_LLM_OUTPUT_LIMBS=8192 # max field limbs for LLM output
ZK_TREE_HEIGHT=8             # Merkle tree height (must match corpus build)
ZK_MERKLE_CAP_HEIGHT=4       # MerkleCap height
ZK_MAX_K=5                   # max chunks per proof
```

### 4.2 Directory Layout

A fresh system must create these directories before running any component:

```bash
zk-rag/
├── .env                    # environment variables (above)
├── Cargo.toml
├── src/
│   ├── circuits/
│   ├── corpus/            # output: corpus_merkle_tree.json, corpus_merkle_proofs.json
│   ├── data/              # chunk files symlinked or copied from CHUNKS_DIR
│   └── target/            # cargo build output
├── configs/
│   └── circuit_config.json
└── scripts/
    └── build_corpus.sh
```

On a fresh system:
1. Clone the repo
2. Copy `.env.example` → `.env` and fill in values
3. Create `src/`, `corpus/`, `configs/` directories
4. Link or copy chunk files to the directory referenced by `CHUNKS_DIR`
5. Run `cargo build` — Rust toolchain required (see `rust-toolchain` file)
6. Run each PRD phase in order (Phase 1 → Phase 2 → Phase 3 → ...)

### 4.3 Portability Notes

- **Chunk paths**: Never hardcode `/data/rag/chunks/` in source code — always read from `CHUNKS_DIR` env var
- **Corpus output**: The Merkle tree build output (`corpus_merkle_tree.json`, `corpus_merkle_proofs.json`) is portable across machines as long as `CHUNKS_DIR` and the chunk content are identical
- **plonky2 version**: Pinned to `v0.2.2` in `Cargo.toml` — changing the version will break circuit compatibility
- **Proof compatibility**: A proof generated on one machine only verifies against the same `CircuitData` (common + verifier_only JSON) — these must be published alongside proofs
- **Kurier**: The `circuit_id` in Kurier must match the `circuit_id` used during proof submission — confirm with Kurier documentation

---

## 5. Reference Implementations

### Ralph Implementation Guide

This section tells Ralph exactly what to do during the Ralph loop. Follow phases in order. Copy means clone verbatim; modify means adapt for this project's data structures; write means implement new code.

---

#### Phase 1 Tasks (PRD-01: Merkle Corpus)

**Copy from plonky2 Merkle tree tutorial:**
- `circuit_tutorials/plonky2/merkle_tree/circuit/src/merkle_tree.rs` → `src/circuits/merkle_tree.rs`
- This gives you `MerkleTree::build()` and `MerkleTree::get_merkle_proof()` which are the canonical implementation
- **Modify**: Change the leaf type from `HashOut<F>` to `HashOut<F>` (already correct for our Poseidon use case)
- Reference: `plonky2/src/hash/merkle_tree.rs` for the underlying `MerkleTree` and `MerkleCap` types

**Write new:**
- `src/corpus.rs` — `CorpusMerkleStore` struct and `build_corpus_merkle_tree(chunks_dir) -> CorpusMerkleStore`
  - Load all `.txt` files from `CHUNKS_DIR` (env var)
  - Sort chunks deterministically by `PoseidonHash(chunk_bytes)` before building tree
  - Produce `cap: Vec<HashOut<F>>` (length = `2^TREE_HEIGHT / 2^MERKLE_CAP_HEIGHT` — default 16)
  - Produce `corpus_merkle_proofs.json` — map of `doc_id:chunk_index -> {index, siblings[]}`
- `src/commands.rs` — CLI command `zk-rag build-tree --output corpus_merkle_tree.json`
- Tests: `tests/test_corpus.rs` — cover empty, single, power-of-2, non-power-of-2 chunk counts

**Env vars used**: `CHUNKS_DIR`, `ZK_TREE_HEIGHT`, `ZK_MERKLE_CAP_HEIGHT`

---

#### Phase 2 Tasks (PRD-02: ZK-RAG Circuit)

**Copy from plonky2 Merkle tree tutorial:**
- `circuit_tutorials/plonky2/merkle_tree/circuit/src/lib.rs` → `src/circuits/zk_rag_circuit.rs` (as starting template)
- The tutorial shows the correct pattern for `CircuitBuilder`, `PartialWitness`, `prove()`, `verify()`

**Modify from tutorial template:**
- Write the ZK-RAG circuit constraint logic:
  - Public inputs: `cap[0..15]`, `llm_input_hash`, `output_hash`
  - Per-chunk: `verify_merkle_proof_to_cap(chunk_hash, index_bits, cap, siblings)` + `PoseidonHash(chunk_text) == chunk_hash`
  - LLM input: `PoseidonHash(llm_input_text) == llm_input_hash`
  - LLM output: `PoseidonHash(llm_output_text) == output_hash`
- Use `builder.hash_n_to_hash_no_pad::<PoseidonPermutation>()` for text-to-hash (confirmed plonky2 API)
- Use `builder.connect()` for equality constraints

**Write new:**
- `src/circuits/zk_rag_circuit.rs` — `ZkRagCircuit`, `ZkRagWitness<K>`, `ChunkWitness`, `build_zk_rag_circuit::<K>()`
- `src/witness.rs` — `assemble_witness()` — combines retrieval output + LLM output into `ZkRagWitnessInput`
- `src/prove.rs` — `prove_locally(witness)` → `ProofWithPublicInputs`
- `src/verify.rs` — `verify_local(proof, circuit_data)` using `CircuitData::verify()`
- `src/serialization.rs` — serialize `CircuitData` and `ProofWithPublicInputs` to JSON
- Tests: `tests/test_zk_rag_circuit.rs` — prove + verify with known input

**Constants** (must match across all phases):
```
ZK_MAX_K = 5              // max chunks per proof
ZK_TREE_HEIGHT = 8       // must match corpus build
ZK_MAX_CHUNK_LIMBS = 8192
ZK_MAX_LLM_INPUT_LIMBS = 16384
ZK_MAX_LLM_OUTPUT_LIMBS = 8192
```

---

#### Phase 3 Tasks (PRD-03 + PRD-04: RAG + LLM Integration)

**Copy from existing codebase:**
- Use existing Qdrant client at the existing Qdrant endpoint
- Use existing Ollama client at the existing Ollama endpoint

**Write new:**
- `src/rag.rs` — `retrieve_chunks_with_proofs(query, k, corpus_store) -> Vec<RetrievedChunk>`
  - Embed query via Ollama `embeddings` API → get embedding vector
  - Query Qdrant with `top_k=k` → get `chunk_id` payloads
  - For each `chunk_id`: load chunk text from `{CHUNKS_DIR}/{doc_id}_{chunk_index}.txt`
  - Attach Merkle proof from `corpus_merkle_proofs.json`
- `src/llm.rs` — `generate_with_ollama(query, chunks, model) -> String`
  - Build prompt from query + chunks (format documented in PRD-04)
  - Call `POST ${OLLAMA_BASE_URL}/api/generate`
  - Return raw response text
- `tests/test_rag.rs`, `tests/test_llm.rs`

**Env vars used**: `CHUNKS_DIR`, `QDRANT_URL`, `QDRANT_COLLECTION`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`

---

#### Phase 4 Tasks (PRD-06: Kurier Verification)

**Write new:**
- `src/kurier.rs` — `verify_via_kurier(proof_json, public_inputs_json) -> Result`
- Local verify is already done in Phase 2 (`src/verify.rs`)
- Kurier verify: `POST ${KURIER_BASE_URL}/verify` with proof + public inputs
- Graceful fallback: if `KURIER_API_KEY` is empty, use local-only

**Env vars used**: `KURIER_BASE_URL`, `KURIER_API_KEY`, `KURIER_CIRCUIT_ID`

---

#### Phase 5 Tasks (PRD-07 + PRD-08: Aggregation + E2E)

**Write new:**
- `src/aggregation.rs` — `build_aggregation_circuit()`, `prove_aggregation()`, `verify_aggregation()`
- `tests/test_e2e.rs` — full pipeline test with small corpus (10 chunks)
- `tests/test_benchmark.rs` — benchmark proving time with corpus sizes [100, 1000]

**Verify plonky2 recursion APIs** before implementing — see PRD-07 Section 6 for the exact files and methods to confirm.

---

### plonky2 v0.2.2 (github.com/0xPolygonZero/plonky2)

| File | Purpose |
|------|---------|
| `plonky2/examples/fibonacci_serialization.rs` | Basic circuit + JSON serialization of proof/circuit data |
| `plonky2/src/hash/merkle_tree.rs` | `MerkleTree`, `MerkleCap<F,H>`, `cap.flatten()` |
| `plonky2/src/hash/merkle_proofs.rs` | `MerkleProof`, `verify_merkle_proof_to_cap()` |
| `plonky2/src/plonk/circuit_builder.rs` | `CircuitBuilder`, `add_virtual_hash_public_input()`, `register_public_input()`, `build()` |
| `plonky2/src/plonk/circuit_data.rs` | `CircuitData::prove()`, `CircuitData::verify()`, `PartialWitness` |

Key plonky2 patterns:
```rust
// Define circuit
type C = PoseidonGoldilocksConfig;
type F = <C as GenericConfig<D>>::F;
const D: usize = 2;

let config = CircuitConfig::standard_recursion_config();
let mut builder = CircuitBuilder::<F, D>::new(config);

// Public inputs
let cap_hash = builder.add_virtual_hash_public_input();
builder.register_public_input(cap_hash);

// Circuit constraints
let leaf_data = builder.add_virtual_targets(n);  // chunk bytes as field elements
// ... build constraints ...

// Build and prove
let data = builder.build::<C>();
let pw = PartialWitness::new();
pw.set_target(cap_hash, cap_value);
let proof = data.prove(pw)?;
data.verify(proof)?;

// Serialize
serde_json::to_string(&proof)?;
serde_json::to_string(&data.common)?;  // for verifier
```

### plonky2 Merkle Tree Tutorial (Reference Implementation)
**Repository**: `github.com/Sindri-Labs/sindri-resources`
**Path**: `circuit_tutorials/plonky2/merkle_tree/` (merged PR #98 by Roee-87, Sep 2024)

Canonical working plonky2 v0.2.2 example. Use as the reference for circuit structure and proving API.

| File | Purpose |
|------|---------|
| `circuit_tutorials/plonky2/merkle_tree/circuit/src/lib.rs` | `MerkleTreeCircuit::prove()` — full plonky2 proving pipeline |
| `circuit_tutorials/plonky2/merkle_tree/circuit/src/merkle_tree.rs` | `MerkleTree::build()`, `MerkleTree::get_merkle_proof()`, `verify_merkle_proof()` |
| `circuit_tutorials/plonky2/merkle_tree/circuit/sindri.json` | plonky2 circuit manifest — `plonky2Version: "0.2.2"`, `circuitType: "plonky2"` |
| `circuit_tutorials/plonky2/merkle_tree/input_1024.json` | Example input with 1024 leaves + index |

**Key code patterns from the tutorial:**
```rust
// ciruit/src/lib.rs — proving with plonky2
let tree: MerkleTree = MerkleTree::build(leaves.clone());
let merkle_proof = tree.clone().get_merkle_proof(prove_leaf_index);
let (circuit_data, targets) = verify_merkle_proof_circuit(prove_leaf_index, nr_layers);

let mut pw = PartialWitness::new();
pw.set_hash_target(targets[0], tree.tree[0][prove_leaf_index]);
for i in 0..nr_layers {
    pw.set_hash_target(targets[i + 1], merkle_proof[i]);
}
let proof_with_pis = circuit_data.prove(pw).unwrap();
data.verify(proof_with_pis.clone()).unwrap();

// sindri.json manifest
{
  "name": "merkle_tree_circuit",
  "circuitType": "plonky2",
  "plonky2Version": "0.2.2",
  "provingScheme": "plonky2",
  "structName": "merkle_tree::MerkleTreeCircuit"
}
```

The tutorial uses a direct Poseidon hashing approach (not the higher-level `verify_merkle_proof_to_cap` from plonky2's stdlib) — the circuit manually hashes leaf + siblings per level using `PoseidonHash::two_to_one()`.

### Hashcloak plonky2 Merkle Trees (Merkle implementation source)
**Repository**: `github.com/hashcloak/plonky2-merkle-trees`
**Path**: `src/simple_merkle_tree/simple_merkle_tree.rs`

The `MerkleTree::build()` and `MerkleTree::get_merkle_proof()` code in the plonky2 Merkle tree tutorial was cloned from this repo. The Hashcloak implementation is the authoritative source for the Merkle tree construction algorithm used.

### Kurier
- Docs: `docs.kurier.xyz` — API format unconfirmed
- Base URL: `api.kurier.xyz` — unconfirmed; structure shown in PRD-06 is illustrative
- Verifies proofs without running full plonky2 verifier locally

---

## 6. Project Structure

```
zk-rag/
├── Cargo.toml
├── src/
│   ├── lib.rs
│   ├── circuits/
│   │   ├── mod.rs
│   │   ├── merkle.rs      # Merkle tree construction + cap serialization
│   │   └── zk_rag.rs      # Main ZK-RAG circuit definition
│   ├── prove.rs           # Proof generation (local)
│   ├── verify.rs          # Verification (local + Kurier)
│   ├── embed.rs           # Chunking + embedding (reuse Qdrant pipeline)
│   └── cli.rs             # CLI: build-tree, prove, verify
├── circuits/
│   └── tests/
│       └── merkle_tests.rs
├── configs/
│   └── circuit_config.json
└── scripts/
    └── build_corpus.sh
```

---

## 7. Security Considerations

1. **Corpus Immutability**: Once root is published, corpus is append-only. Rebuilding with different docs produces a different root—old proofs become invalid.
2. **Chunk Sorting**: Sort chunks deterministically (by `PoseidonHash(chunk_bytes)`) before building tree. Non-deterministic ordering breaks verification.
3. **Query Privacy**: Query text is NOT hidden from the verifier. Only document contents and the full LLM input are private.
4. **Merkle Path Leakage**: Revealing sibling hashes for a chunk does not reveal other corpus chunks—Merkle trees are information-theoretically secure.
5. **No Private Keys**: Pure proving system; no signatures or secret keys involved.
6. **ZK Proof Soundness**: plonky2 with Poseidon is a proof system with extractable witness—anyone with the proof can extract the private inputs. This is standard for STARKs; do not confuse with FHE or secure multi-party computation.

---

## 8. Project Phases

### Phase 1: Foundation — Merkle Corpus + Embedding
- Build Merkle tree from existing chunked documents in `/data/rag/chunks/`
- Use existing Qdrant vector DB for top-K retrieval
- Extract and store the MerkleCap (root) for the corpus
- **Publish Merkle root to Horizen EVM contract** (one root per corpus snapshot; append new roots as corpus grows)
- Verify that retrieval returns chunks that map to valid Merkle proofs

### Phase 2: ZK Circuit — plonky2 Implementation
- Implement `zk_rag_circuit` in plonky2 0.2.2
- Circuit proves: "these K chunks exist in the Merkle tree with this root"
- Local proof generation for development testing
- Unit tests with small synthetic corpus

### Phase 3: Local Proving
- Local plonky2 proof generation using `plonky2::plonk::circuit_data::CircuitData::prove()`
- Unit tests with small synthetic corpus
- Compare local proof output against reference tutorial proof (compatibility check)

### Phase 4: Verification Layer — Kurier / zkVerify Integration
- Integrate Kurier `/verify` endpoint (zkVerify on-chain proof verification)
- Fallback: local `CircuitData::verify()` always works
- Verify first proof end-to-end

### Phase 5: E2E Integration + Benchmarking
- Connect full RAG pipeline: embed → retrieve → LLM → proof → verify
- End-to-end test with real query and corpus
- Benchmark local CPU proving times with small corpus

---

## 9. Open Questions (for Mr. V)

1. **Kurier API**: Base URL and `/verify` endpoint format need confirmation from `docs.kurier.xyz`
2. **Initial corpus**: Is the corpus the existing military doctrine docs in `/data/rag/chunks/`?
3. ~~On-chain publication~~: ✅ Merkle root published to Horizen EVM contract at ingestion (ZEND); new roots appended per corpus snapshot. Proof verification via Kurier / zkVerify at query time.
4. **Corpus update cadence**: How often does corpus change? New corpus = new circuit or just new root?
5. **Performance targets**: Acceptable proving time bounds?
6. **ZK Circuit Authoring**: Should Ralph write the plonky2 circuit code during the Ralph loop?
7. **Horizen EVM contract**: Who owns/writes the `CorpusRegistry` contract? Do we have a contract address already?
