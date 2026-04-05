# PRD-07: Proof Aggregation and Recursive Proving

## 1. Background and Context

A single ZK-RAG proof verifies K chunks and an LLM output. For production use at scale, we may want to aggregate multiple ZK-RAG proofs into a single proof using recursive SNARK composition. This also enables compressing multiple STARKs into a single proof that is cheaper to verify on-chain.

This PRD covers the recursive proof aggregation strategy using plonky2's native recursion, and the circuit design for proof composition.

**Note**: This is an advanced Phase 3 feature. The initial implementation (Phase 1-2) generates a single non-recursive proof per query. This PRD captures the design for Phase 3 extensibility.

## 2. Goals and User Stories

**As a** ZK-RAG operator,
**I want** to aggregate multiple query proofs into a single proof
**so that** I can verify many queries simultaneously at lower on-chain cost.

**As a** on-chain verifier,
**I want** a single proof to verify instead of N separate proofs
**so that** gas costs are reduced.

## 3. Functional Requirements

### FR-1: Recursive Proof Composition

Use plonky2's native proof recursion to nest ZK-RAG proofs:

**IMPORTANT**: These plonky2 recursion APIs must be confirmed against `plonky2/src/plonk/` at v0.2.2 before implementation. The exact API for recursive proof verification may differ from the pattern shown below.

```rust
// Wrap a ZK-RAG proof as a circuit input
// Reference: plonky2 recursion pattern from plonky2/src/plonk/recursive_verifier.rs
pub fn build_aggregation_circuit(
    builder: &mut CircuitBuilder<F, D>,
    inner_proofs: &[ProofWithPublicInputsTarget<D>],  // N proofs
) -> (PublicInputTarget, PublicInputTarget) {
    // Verify each inner proof recursively
    // Note: The exact method name and signature for recursive proof verification
    // must be confirmed from plonky2 v0.2.2 source at:
    // plonky2/src/plonk/recursive_verifier.rs
    // Common patterns use CircuitBuilder::add_proof_with_pub_inputs() and
    // register verification constraints on the inner_proof targets.
    for inner_proof in inner_proofs.iter() {
        builder.verify_outer_proof(inner_proof, &aggregation_vd, &aggregation_common);
    }
    // Output: single aggregated public input (hash of all inner public inputs)
    let agg_hash = builder.hash_n_to_hash_no_pad(inner_pis_concat);
    (agg_hash, ...)
}
```

### FR-2: Proof Padding (Variable Number of Queries)

- Support aggregating N proofs where N ∈ {1, 2, 4, 8, 16}
- Pad to next power of 2 by duplicating the last proof
- Aggregation circuit is parameterized by N at build time

### FR-3: On-chain Verification

```rust
// Generate Solidity verifier from plonky2 circuit
// Note: The exact API for Solidity code generation must be confirmed at plonky2 v0.2.2.
// The pattern below is based on common plonky2 APIs; verify before implementing.
use plonky2::plonk::circuit_data::CircuitData;

// After building aggregation circuit:
// Option A — if the to_solidity() API exists:
let sol_code = data.verifier_only.to_solidity(&data.common);
std::fs::write("AggregationVerifier.sol", sol_code)?;

// Option B — if a different API is used:
let verifier_contract = data.switch_prover_verifier();
let sol_code = verifier_contract.to_solidity();
std::fs::write("AggregationVerifier.sol", sol_code)?;
```

- plonky2 can generate a Solidity verifier contract — exact API to confirm at v0.2.2
- Deploy verifier to Ethereum L2 (Arbitrum/Base) for public on-chain verification
- The contract implements the verifier interface expected by Kurier (confirm Kurier's expected interface)

### FR-4: Batch Verification (Without Recursion)

Before implementing full recursion, implement cheap batch verification:

```rust
// Note: CircuitData::verify(proof) takes only the proof.
// The verifier_only and common are embedded in CircuitData at build time.
// Correct pattern (verified against plonky2 v0.2.2 circuit_data.rs:198):
pub fn batch_verify_local(
    proofs: &[ProofWithPublicInputs<F, C, D>],
    _verifier_only: &VerifierOnlyCircuitData<F, C, D>,
    _common: &CommonCircuitData<F, C, D>,
) -> Result<(), Error> {
    for proof in proofs.iter() {
        // CircuitData::verify takes only the proof.
        // The CircuitData (which holds verifier_only + common) is stored
        // in the VerificationService and used directly.
        verify_proof_local(proof)?;
    }
    Ok(())
}
```

- Verify all proofs locally; return first failure
- Useful for off-chain batch verification

## 4. Circuit Design (Aggregation)

**Aggregation Circuit Public Inputs:**
- `inner_proofs_pis_hash` — hash of concatenated public inputs from all inner proofs
- `aggregation_cap[16]` — Merkle cap of all documents across all queries (optional, for cross-query claims)

**Aggregation Circuit Private Inputs:**
- Inner proofs (serialized as bytes or as proof targets)

**Constraints:**
- For each inner proof: `verify_outer_proof(inner_proof)` — plonky2 recursion gate
- All inner proofs must be valid

## 5. Edge Cases

| Scenario | Handling |
|----------|----------|
| N=1 (no aggregation needed) | Skip aggregation circuit; use single proof directly |
| Inner proof invalid | Aggregation circuit fails constraint; outer proof invalid |
| N > max supported | Chunk into groups of max_size; aggregate groups recursively |
| Proof size exceeds recursion limit | plonky2 recursion depth limit is ~100; unlikely to hit |

## 6. Environment and Portability

See **Section 4 (Portability and System Setup)** in the project plan for the full `.env` schema.

This PRD depends on plonky2 v0.2.2 internals. The following must be verified by reading the actual source before implementation:

| File | What to confirm |
|------|----------------|
| `plonky2/src/plonk/recursive_verifier.rs` | Exact API for recursive proof verification (`verify_outer_proof` or equivalent) |
| `plonky2/src/plonk/circuit_data.rs` | `CircuitData::verify()` signature and module location |
| `plonky2/src/plonk/mod.rs` | Whether `to_solidity()` exists and on which type |

## 7. Acceptance Criteria

- [ ] `build_aggregation_circuit::<4>` produces valid circuit for N=4 proofs
- [ ] Aggregation proof verifies against its own public inputs
- [ ] Inner proofs are extractable from aggregation proof witness (for auditing)
- [ ] Batch verification completes N=100 proofs with one function call
- [ ] Solidity verifier code is generated and syntactically valid
- [ ] Unit tests: generate N=2 aggregation proofs; verify individually + aggregated
