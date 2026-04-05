# PRD-08: End-to-End Integration Testing and Benchmarking

## 1. Background and Context

ZK-RAG is a multi-component system. Each PRD is individually testable, but the real validation is an end-to-end run: a real user query arrives, chunks are retrieved from the real corpus with real Merkle proofs, an LLM generates a grounded response, a ZK proof is generated, and the proof is verified. This PRD defines the E2E test harness and benchmarking framework.

## 2. Goals and User Stories

**As a** operator,
**I want** an automated E2E test that runs a complete ZK-RAG cycle
**so that** I can validate the entire pipeline before deployment.

**As a** developer,
**I want** benchmark results for local proving with different corpus sizes
**so that** I can understand performance characteristics.

## 3. Functional Requirements

### FR-1: E2E Test Harness

```rust
pub async fn run_e2e_test(config: &E2eConfig) -> E2eResult {
    // Step 1: Load corpus
    let corpus_store = build_corpus_merkle_tree(&config.chunks_dir)?;

    // Step 2: Run query
    let retrieved = retrieve_chunks_with_proofs(
        &config.query,
        config.k,
        &corpus_store,
    ).await?;

    // Step 3: Build prompt + call LLM
    let llm_input = build_rag_prompt(&config.query, &retrieved);
    let llm_output = generate_with_ollama(&llm_input.full_prompt, &config.model).await?;

    // Step 4: Assemble witness
    let witness = assemble_witness(retrieved, &llm_input, &llm_output, &corpus_store)?;

    // Step 5: Generate proof (local only)
    let proof = prove_locally(&witness)?;

    // Step 6: Verify proof
    let verify_result = verification_service.verify(
        &proof.proof_json,
        &proof.public_inputs,
    ).await?;

    E2eResult {
        query: config.query.clone(),
        chunks_retrieved: retrieved.len(),
        llm_output_hash: witness.output_hash,
        proving_time_ms: proof.elapsed_ms,
        verification_result: verify_result,
        circom_circuit_name: "zk_rag".into(),
    }
}
```

### FR-2: Benchmark Suite

```rust
pub struct BenchmarkConfig {
    pub corpus_sizes: Vec<usize>,  // [100, 1000, 10000]
    pub k_values: Vec<usize>,      // [1, 3, 5]
    pub proving_modes: Vec<ProvingMode>,
    pub queries_per_config: usize,
}

pub struct BenchmarkResult {
    pub config: BenchmarkConfig,
    pub avg_proof_time_ms: f64,
    pub p50_proof_time_ms: f64,
    pub p99_proof_time_ms: f64,
    pub avg_verification_time_ms: f64,
    pub proof_size_bytes: usize,
}
```

Metrics to collect:
- **Proof generation time**: wall-clock time from witness submission to proof receipt
- **Proof verification time**: wall-clock time for Kurier/local verification
- **Proof size**: serialized JSON bytes for proof object
- **Local memory usage**: peak RSS during local proving
- **Qdrant retrieval latency**: time to get top-K chunks
- **Ollama generation time**: time to first token + total generation

### FR-3: Synthetic Corpus Generator

For benchmarks at scale, generate synthetic documents:

```rust
pub fn generate_synthetic_corpus(size: usize, avg_chunk_words: usize) -> TempDir {
    // Create temp directory with size random chunk files
    // Chunk content: Lorem ipsum-like with randomized entity names
}
```

- Chunk files written to temp directory
- Merkle tree built from temp directory
- Useful for testing at 1K, 10K, 100K chunk scales without real documents

### FR-4: Regression Test Suite

```rust
// test_e2e_regression.rs
#[tokio::test]
async fn test_zkrag_e2e_small_corpus() {
    // Use a small real corpus (10 chunks from CHUNKS_DIR)
    // Run full pipeline; assert proof verifies
}

#[tokio::test]
async fn test_zkrag_e2e_no_chunks() {
    // K=0 case; LLM generates without context
    // Proof should still be generable (even if degenerate)
}

#[tokio::test]
async fn test_zkrag_e2e_duplicate_chunks() {
    // Two identical chunks retrieved
    // Both must have valid separate Merkle proofs
}
```

### FR-5: Test Configuration

```yaml
# test.yml
e2e:
  chunks_dir: ${CHUNKS_DIR}
  corpus_size: 100  # use subset for CI
  k: 3
  query: "What is the target audience for this doctrine?"
  model: ${OLLAMA_MODEL:-llama3}

benchmarks:
  corpus_sizes: [100, 1000]
  k_values: [3, 5]
  queries_per_config: 10
```

## 4. CI Integration

- E2E regression tests run on every PR (using small corpus to keep CI fast)
- Benchmark suite runs nightly; results published to log

## 5. Environment and Portability

See **Section 4 (Portability and System Setup)** in the project plan for the full `.env` schema. This PRD is the integration layer — it requires all upstream PRDs to be complete.

| Variable | Required | Description |
|----------|----------|-------------|
| `CHUNKS_DIR` | Yes | Chunk files directory |
| `QDRANT_COLLECTION` | Yes | Qdrant collection name |
| `OLLAMA_MODEL` | Yes | Ollama model name |
| `ZK_MAX_K` | Yes | Max chunks per proof (must match circuit build) |

## 7. Acceptance Criteria

- [ ] `run_e2e_test()` completes a full pipeline run and returns `verification_result: Verified`
- [ ] E2E test fails gracefully with informative error if any component fails
- [ ] Benchmark suite produces CSV output with all metrics defined above
- [ ] Synthetic corpus at 10K chunks can be built and queried in < 5 minutes
- [ ] Regression test suite covers: normal case, K=0, duplicate chunks, empty corpus, Qdrant down
- [ ] All E2E tests are annotated with `#[tokio::test]` and runnable via `cargo test`
- [ ] Proving time for K=5, corpus=1000 on local CPU is recorded (baseline metric)
