# PRD-02: ZK-RAG plonky2 Circuit

## 1. Background and Context

The ZK-RAG circuit is the core zero-knowledge proof component. It is a plonky2 (v0.2.2) arithmetic circuit that constrains a prover to prove, for a given set of public inputs, that they know K document chunks that:
1. Each hash to a value that is a member of the committed Merkle tree (root = cap)
2. The chunks were included in the LLM's input context (proven via hash chain)
3. The final LLM output text hashes to the declared output digest

The circuit produces a STARK proof that can be verified against the public inputs only.

## 2. Goals and User Stories

**As a** ZK-RAG prover,
**I want** a plonky2 circuit that proves my retrieved chunks are in the corpus
**so that** I can generate a verifiable proof without revealing the chunks.

**As a** verifier,
**I want** to check a proof using only the public inputs
**so that** I can confirm the prover used an authentic corpus without seeing it.

## 3. Functional Requirements

### FR-1: Circuit Definition (`zk_rag_circuit`)

Define a plonky2 `CircuitBuilder` circuit with the following structure:

**Public Inputs (PI):**
1. `cap[0..15]` — 16 x `PoseidonHash` = MerkleCap (corpus root commitment)
2. `llm_input_hash` — 1 x `PoseidonHash` = hash of the full LLM input text (query + all chunks)
3. `output_hash` — 1 x `PoseidonHash` = hash of the full LLM output text

**Private Inputs (Witness):**
For each of K chunks (K ≤ 5, configurable at circuit build time):
4. `chunk_hash[i]` — PoseidonHash of chunk i's text (committed by prover; recomputed and constrained in circuit)
5. `chunk_index[i]` — integer index of chunk in Merkle tree (bits as `BoolTarget`)
6. `merkle_proof_siblings[i]` — array of `HashOutTarget` (sibling hashes per level)
7. `chunk_text[i]` — raw bytes of chunk as array of field element targets (limbs)

Additional witness:
8. `llm_output_text` — bytes of the LLM output as field element limbs

**Constraints:**
- For each chunk i: `verify_merkle_proof_to_cap(chunk_hash[i], index_bits[i], cap, proof[i])`
- For each chunk i: `PoseidonHash(chunk_text[i]) == chunk_hash[i]`
- `PoseidonHash(llm_input_text) == llm_input_hash` — binds the prover's private input text to the public commitment
- `PoseidonHash(llm_output_text) == output_hash`

### FR-2: Configurable Chunk Count (K)
- Circuit template takes K as a compile-time constant
- Support K ∈ {1, 3, 5} — three separate circuit builds
- The number of chunks used in a specific proof is determined by witness assignment (unused chunks set to zero/empty)

### FR-3: Poseidon Hashing
- Use plonky2's built-in Poseidon permutation (`PlonkyPermutation`)
- Hash function: `PoseidonHash(bytes_limbs) -> HashOut` (4 field elements)
- `two_to_one(left, right) -> HashOut` used for Merkle tree internal nodes

### FR-4: Local Proof Generation
- Implement `prove_locally(circuit_data, witness) -> ProofWithPublicInputs`
- Use plonky2's `PartialWitness` to assign all targets
- Serialize proof to JSON for submission to cloud provers

### FR-5: Circuit Serialization
- Serialize `CircuitData.common` to JSON (verifier contract / public params)
- Serialize `CircuitData.verifier_only` to JSON (short verifier data)
- These are needed by the verifier; do NOT serialize the prover-only data

## 4. Data Structures

```rust
use plonky2::field::types::Field;
use plonky2::hash::hash_types::{HashOut, HashOutTarget};
use plonky2::iop::target::Target;
use plonky2::iop::witness::PartialWitness;
use plonky2::plonk::circuit_builder::CircuitBuilder;
use plonky2::plonk::circuit_data::CircuitData;
use plonky2::plonk::config::{GenericConfig, PoseidonGoldilocksConfig};

const D: usize = 2;
type C = PoseidonGoldilocksConfig;
type F = <C as GenericConfig<D>>::F;

struct ZkRagWitness<const K: usize> {
    cap: [HashOut<F>; 16],         // PI[0..15]
    llm_input_hash: HashOut<F>,    // PI[16..19]
    output_hash: HashOut<F>,       // PI[20..23]
    chunks: Vec<ChunkWitness>,     // witness (K chunks)
    llm_input_text: Vec<F>,        // witness: full prompt text as field limbs
    llm_output_text: Vec<F>,       // witness: output text as field limbs
}

struct ChunkWitness {
    hash: HashOut<F>,
    index: usize,
    merkle_siblings: Vec<HashOut<F>>,
    text_bytes: Vec<F>,
    /// Defined in PRD-03 (RAG Embedding Integration).
    /// Produced by `retrieve_chunks_with_proofs()` and used here as circuit witness.
}
```

## 5. Circuit Builder Pattern

```rust
fn build_zk_rag_circuit<const K: usize>(
    builder: &mut CircuitBuilder<F, D>,
) {
    // Public inputs
    let cap = builder.add_virtual_cap(4);             // 16 x HashOutTarget — PI[0..15]
    let llm_input_hash = builder.add_virtual_hash_public_input(); // PI[16..19]
    let output_hash = builder.add_virtual_hash_public_input();     // PI[20..23]

    // LLM input text witness + constraint
    let llm_input_text: Vec<Target> = builder.add_virtual_targets(MAX_LLM_INPUT_LIMBS);
    let llm_input_recomputed = builder.hash_n_to_hash_no_pad::<PoseidonPermutation>(&llm_input_text);
    builder.connect(llm_input_recomputed, llm_input_hash);

    // LLM output text witness + constraint
    let llm_output_text: Vec<Target> = builder.add_virtual_targets(MAX_LLM_OUTPUT_LIMBS);
    let llm_output_recomputed = builder.hash_n_to_hash_no_pad::<PoseidonPermutation>(&llm_output_text);
    builder.connect(llm_output_recomputed, output_hash);

    // Per-chunk constraints
    for i in 0..K {
        // Private witness: chunk text limbs
        let chunk_text_limbs: Vec<Target> = builder.add_virtual_targets(MAX_CHUNK_LIMBS);

        // Private witness: hash of this chunk (committed by prover)
        let chunk_hash: HashOutTarget = builder.add_virtual_hash();

        // Merkle proof verification using private witness
        let leaf_index_bits = /* array of BoolTarget from index */;
        let siblings: Vec<HashOutTarget> = (0..TREE_HEIGHT).map(|_| builder.add_virtual_hash()).collect_vec();
        builder.verify_merkle_proof_to_cap::<PoseidonHash>(
            chunk_hash.clone(),
            leaf_index_bits,
            &cap,
            &MerkleProofTarget { siblings },
        );

        // Rehash the chunk text and constrain to the claimed chunk_hash
        let recomputed = builder.hash_n_to_hash_no_pad::<PoseidonPermutation>(&chunk_text_limbs);
        builder.connect(recomputed, chunk_hash);
    }
}
```

**Critical design notes:**
- `llm_input_hash` is a **public input** — the prover cannot claim a false prompt commitment
- `llm_output_hash` is a **public input** — the output is publicly committed
- `chunk_hash[i]` is **private witness** — only revealed via Merkle proof + output commitment
- The circuit uses `builder.connect()` to enforce equality between recomputed and claimed hashes
- `MAX_LLM_INPUT_LIMBS`, `MAX_LLM_OUTPUT_LIMBS`, `MAX_CHUNK_LIMBS` must be defined as constants; text exceeding these limits must be handled outside the circuit (truncated or hashed differently)

**Note**: The plonky2 `CircuitBuilder` does not expose a direct `hash_bytes_to_poseidon` method. The actual implementation must use `builder.hash_n_to_hash_no_pad::<PoseidonPermutation>(leaf_targets)` over the byte limb targets. The exact API for splitting bytes into field limbs and hashing them must be confirmed by reviewing `plonky2/src/plonk/circuit_builder.rs` at v0.2.2.

**Reference Implementation**: plonky2 v0.2.2 Merkle proof circuit at:
`github.com/Sindri-Labs/sindri-resources` → `circuit_tutorials/plonky2/merkle_tree/circuit/src/lib.rs`

Key pattern from the tutorial (manual per-level hashing):
```rust
// From sindri-resources/circuit/src/lib.rs — verify_merkle_proof_circuit()
let leaf_to_prove = builder.add_virtual_hash();
let merkle_proof_elm = builder.add_virtual_hash();

// Hash leaf with sibling at base layer
if leaf_index % 2 == 0 {
    next_hash = builder.hash_or_noop::<PoseidonHash>(
        [leaf_to_prove.elements.to_vec(), merkle_proof_elm.elements.to_vec()].concat(),
    );
} else {
    next_hash = builder.hash_or_noop::<PoseidonHash>(
        [merkle_proof_elm.elements.to_vec(), leaf_to_prove.elements.to_vec()].concat(),
    );
}

// Continue up the tree
for _layer in 1..nr_layers {
    let merkle_proof_elm = builder.add_virtual_hash();
    targets.push(merkle_proof_elm);
    if current_layer_index % 2 == 0 {
        next_hash = builder.hash_or_noop::<PoseidonHash>(
            [next_hash.elements.to_vec(), merkle_proof_elm.elements.to_vec()].concat(),
        );
    } else {
        next_hash = builder.hash_or_noop::<PoseidonHash>(
            [merkle_proof_elm.elements.to_vec(), next_hash.elements.to_vec()].concat(),
        );
    }
    current_layer_index = current_layer_index / 2;
}

// Register root as public input
builder.register_public_inputs(&next_hash.elements);
let data = builder.build::<C>();
```

The tutorial uses `builder.add_virtual_hash()` for both public and private hash targets, and `builder.register_public_inputs()` for the root. This is the circuit pattern to replicate for ZK-RAG.

## 6. Edge Cases

| Scenario | Handling |
|----------|----------|
| Chunk text longer than circuit capacity | Truncate or split into multiple hash constraints; define max chunk size |
| K=0 (no chunks retrieved) | Circuit must still verify; degenerate case, may skip proof |
| Index out of range | Merkle proof verification fails in circuit; proof generation fails |
| All chunks identical | Each occurrence verified independently; no special handling needed |
| Circuit recursion needed for large K | Phase 2 may use recursive aggregation; Phase 1 targets K≤5 single circuit |

## 7. Acceptance Criteria

- [ ] `build_zk_rag_circuit::<5>` produces a valid `CircuitData<F, C, D>`
- [ ] `CircuitData::prove(pw)` succeeds when given valid witness data
- [ ] `CircuitData::verify(proof)` succeeds for a valid proof
- [ ] Proof public inputs match: `cap[16]` + `output_hash`
- [ ] Serialization round-trip: `CircuitData` → JSON → `CircuitData` (common + verifier_only)
- [ ] Unit test: small corpus (10 chunks), K=2, local prove + verify
- [ ] CLI command: `zk-rag prove --circuit-id <id> --witness <witness.json>`
- [ ] The plonky2 version is pinned to `v0.2.2` in `Cargo.toml`
