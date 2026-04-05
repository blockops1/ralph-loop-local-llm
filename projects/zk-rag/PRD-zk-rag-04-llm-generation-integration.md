# PRD-04: LLM Generation Integration

## 1. Background and Context

The ZK-RAG system requires an LLM that generates responses grounded in the retrieved document chunks. The LLM's input (the full prompt, including the query and the retrieved chunks) and output (the generated text) are both inputs to the ZK circuit.

This PRD covers: constructing the LLM prompt from the query and retrieved chunks, calling the LLM via Ollama, capturing the input/output for ZK witness data, and computing the SHA-256/Poseidon hashes needed by the circuit.

## 2. Goals and User Stories

**As a** ZK-RAG prover,
**I want** to run an LLM with retrieved context and capture the exact input/output text
**so that** I can compute the hashes needed for the ZK proof.

**As a** ZK-RAG system,
**I want** the prompt format to be deterministic and documented
**so that** the circuit constraints correctly bind the LLM's input to its output.

## 3. Functional Requirements

### FR-1: Prompt Construction

```rust
pub struct LlmInput {
    pub full_prompt: String,     // Exact prompt sent to LLM
    pub query: String,           // Original user query
    pub chunks: Vec<String>,     // Retrieved chunk texts
}

pub fn build_rag_prompt(query: &str, chunks: &[RetrievedChunk]) -> LlmInput;
```

**Prompt format** (deterministic, documented):
```
<system>
You are a helpful assistant. Answer the question using only the provided context documents. Cite sources by chunk ID when using specific information. If the context does not contain the answer, say so.
</system>

<context>
[Chunk 1 - ID: {chunk_id_1}]
{chunk_text_1}

[Chunk 2 - ID: {chunk_id_2}]
{chunk_text_2}

... (up to K chunks)
</context>

<query>
{user_query}
</query>

<answer>
```

### FR-2: Ollama Integration

```rust
pub async fn generate_with_ollama(
    prompt: &str,
    model: &str,
) -> Result<String, Error>;
```

- Use Ollama API: `POST ${OLLAMA_BASE_URL}/api/generate` (env var — default `http://localhost:11434`)
- Model: configurable; default from environment `OLLAMA_MODEL`
- Parameters: `temperature=0.3`, `num_ctx=8192` (large enough for chunks)
- Capture exact response text including any trailing whitespace
- Timeout: 120 seconds

### FR-3: Hash Computation

```rust
pub fn compute_poseidon_hash(text: &str) -> HashOut<F>;
pub fn compute_llm_input_hash(prompt: &LlmInput) -> HashOut<F>;
pub fn compute_llm_output_hash(response: &str) -> HashOut<F>;
```

- Convert UTF-8 bytes to 64-bit little-endian field elements (Goldilocks field)
- Hash with Poseidon permutation using plonky2's `hash_n_to_hash_no_pad`
- Return `HashOut<F>` (4 field elements)

### FR-4: Witness Data Assembly

```rust
/// Complete witness for the ZK-RAG circuit.
/// Public inputs (PI): cap, llm_input_hash, output_hash — must be set as circuit PI targets.
/// Private witness: llm_input_text, llm_output_text, chunks — set via PartialWitness.
pub struct ZkRagWitnessInput {
    // Public inputs (set via builder.add_virtual_hash_public_input())
    pub cap: Vec<HashOut<F>>,         // 16 root values — MerkleCap commitment
    pub llm_input_hash: HashOut<F>,   // Hash of full LLM input text (prompt + chunks)
    pub output_hash: HashOut<F>,      // Hash of LLM output text

    // Private witness
    pub chunks: Vec<ChunkWitness>,   // From PRD-03 RetrievedChunk
    pub llm_input_text: Vec<F>,       // Full LLM input text as field limbs (for circuit constraint)
    pub llm_output_text: Vec<F>,      // LLM output text as field limbs (for circuit constraint)
}

pub fn assemble_witness(
    retrieved: Vec<RetrievedChunk>,
    llm_input: &LlmInput,
    llm_output: &str,
    corpus_store: &CorpusMerkleStore,
) -> Result<ZkRagWitnessInput, Error>;
```

**Constraint mapping:**
- `output_hash = PoseidonHash(llm_output_text)` → PI (circuit enforces equality)
- `llm_input_hash = PoseidonHash(llm_input_text)` → PI (circuit enforces equality)
- `cap` → PI (from `CorpusMerkleStore`)

**Field limb encoding**: UTF-8 bytes are split into 64-bit little-endian unsigned integers, one per field element. `MAX_LLM_INPUT_LIMBS`, `MAX_LLM_OUTPUT_LIMBS`, `MAX_CHUNK_LIMBS` must be agreed with circuit constants.

### FR-5: Determinism and Exactness

- The prompt must be byte-for-byte identical when reconstructed from the same query + chunks
- Chunk texts are included as-is; no reformatting, no trimming of leading/trailing whitespace that changes the hash
- The exact LLM output text is hashed; if the LLM returns different text on re-run, the proof becomes invalid

## 4. Prompt Determinism Rules

To ensure a proof can be regenerated:
1. Chunks are inserted in descending Qdrant score order (stable sort by score)
2. Chunk IDs are included verbatim
3. XML-like delimiters are standardized: `[Chunk N - ID: {id}]` with no extra spaces
4. The `</answer>` tag is NOT included in the prompt (it is the output delimiter only)
5. Ollama is configured with `seed` parameter set to a fixed value for reproducibility

## 5. Edge Cases

| Scenario | Handling |
|----------|----------|
| Ollama not responding | Return error; do not generate proof |
| LLM output empty | Hash of empty string is valid; proof includes empty output hash |
| LLM output exceeds max | Chunk or truncate; document max output size in circuit config |
| Prompt + chunks exceed context window | Use fewer chunks (K' < K); circuit must support variable K witness |
| Ollama returns non-deterministic text | Set `seed` param; if non-determinism persists, document as known limitation |

## 6. Environment and Portability

See **Section 4 (Portability and System Setup)** in the project plan for the full `.env` schema and directory layout.

| Variable | Required | Description |
|----------|----------|-------------|
| `OLLAMA_MODEL` | Yes | Ollama model name (e.g., `llama3`) |
| `OLLAMA_BASE_URL` | Yes | Ollama server URL (default: `http://localhost:11434`) |
| `ZK_MAX_LLM_INPUT_LIMBS` | Yes | Max field limbs for full prompt (for circuit constraint boundary) |
| `ZK_MAX_LLM_OUTPUT_LIMBS` | Yes | Max field limbs for LLM output |

On a fresh system: Ollama must be installed and running before this PRD's integration tests can run. The `OLLAMA_BASE_URL` must point to a reachable Ollama instance.

## 7. Acceptance Criteria

- [ ] `build_rag_prompt()` produces the exact prompt format documented above
- [ ] `generate_with_ollama()` returns the LLM's raw response text exactly
- [ ] `compute_poseidon_hash()` produces consistent hashes verified against plonky2 test vectors
- [ ] `assemble_witness()` produces a complete `ZkRagWitnessInput` ready for plonky2 `PartialWitness`
- [ ] The prompt format is documented and stable; changing it breaks old proofs
- [ ] Unit tests: mock Ollama; verify prompt format; verify hash computation
- [ ] Integration test: real Ollama call; verify proof generation succeeds with real data
