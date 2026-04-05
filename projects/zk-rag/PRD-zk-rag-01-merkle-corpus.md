# PRD-01: Merkle Corpus Builder

## 1. Background and Context

The ZK-RAG system requires a **deterministic, append-only commitment** to the document corpus. This commitment takes the form of a Merkle tree whose root (specifically, a MerkleCap of height 4) is published and used as the primary public input in all subsequent ZK proofs.

This PRD covers the first subsystem: building the Merkle tree from the existing chunked documents, handling deterministic ordering, and exposing the corpus root and per-chunk Merkle proof data for use by the proving system.

## 2. Goals and User Stories

**As a** ZK-RAG prover,
**I want** a tool that deterministically builds a Merkle tree from my document corpus
**so that** I can commit to a specific version of the corpus and generate proofs against that commitment.

**As a** ZK-RAG verifier,
**I want** an independently reproducible corpus root
**so that** I can confirm proofs were generated against the correct corpus.

## 3. Functional Requirements

### FR-1: Chunk Loading
- Load all text chunks from `CHUNKS_DIR` (environment variable — typically `/data/rag/chunks/` on this system)
- Each chunk file is a plain-text file; filename format: `{doc_id}_{chunk_index}.txt`
- Load all `.txt` files recursively under the chunks directory
- Fail gracefully if the directory is empty or missing

### FR-2: Merkle Tree Implementation (Hashcloak)

The authoritative Merkle tree implementation for plonky2 is from **Hashcloak**:
`github.com/hashcloak/plonky2-merkle-trees` — `src/simple_merkle_tree/simple_merkle_tree.rs`

Same code used in the **plonky2 Merkle tree tutorial**:
`github.com/Sindri-Labs/sindri-resources` — `circuit_tutorials/plonky2/merkle_tree/circuit/src/merkle_tree.rs`

```rust
// Key methods from Hashcloak / plonky2 tutorial
pub fn build(leaves: Vec<GoldilocksField>) -> MerkleTree {
    let level0: Vec<HashOut<GoldilocksField>> = leaves
        .into_iter()
        .map(|leaf| PoseidonHash::hash_or_noop(&[leaf]))
        .collect();
    // Pairwise hash up the tree
    let mut levels = vec![level0];
    for i in 0..(count_levels - 1) {
        levels.push(Self::next_level_hashes(levels[i].clone()));
    }
    let root = PoseidonHash::two_to_one(last_hashes[0], last_hashes[1]);
    MerkleTree { tree: levels, root, count_levels }
}

pub fn get_merkle_proof(self, leaf_index: usize) -> Vec<HashOut<GoldilocksField>> {
    // Returns sibling hash per level — used as circuit witness
    let mut proof_hashes = Vec::new();
    let mut updated_index = leaf_index;
    for i in 0..self.count_levels {
        let selected = if updated_index.is_odd() {
            level_i[updated_index - 1]
        } else {
            level_i[updated_index + 1]
        };
        proof_hashes.push(selected_hash);
        updated_index = updated_index / 2;
    }
    proof_hashes
}
```

### FR-3: Deterministic Sorting
- Sort chunks by `PoseidonHash(chunk_text_bytes)` before building the Merkle tree
- Sorting ensures the tree is deterministic regardless of filesystem ordering
- The sort key is computed once at build time and stored as metadata
- NOTE: Sort key must match the leaf hash function (FR-4) so sorted_index is directly derivable from the leaf hash without a separate lookup table

### FR-4: Merkle Tree Construction
- Use Poseidon hash (plonky2-compatible) as the internal hash function
- Each leaf = `PoseidonHash(chunk_text_bytes)` — bytes split into 64-bit limbs as field elements
- Build a full binary Merkle tree (not sparse); number of leaves rounded up to next power of 2 with zero-filled padding
- Compute and return the MerkleCap at height 4 (16 root values)

### FR-5: Merkle Proof Generation
- For any chunk, generate a `MerkleProof` (list of sibling hashes from leaf to root cap)
- API: `get_merkle_proof(chunk_index: usize) -> MerkleProof`
- The proof is a list of sibling `HashOut` values (Poseidon digest, 4 field elements each)

### FR-6: Serialization and Storage
- Tree structure: stored as a JSON file `corpus_merkle_tree.json`
  - `cap`: array of 16 PoseidonHash values (the MerkleCap)
  - `total_chunks`: integer
  - `tree_height`: integer
  - `chunk_hashes`: array of all leaf hashes in sorted order
- Per-chunk proof: stored in `corpus_merkle_proofs.json` (array indexed by chunk position)
- Rebuild is idempotent: same corpus always produces identical output

### FR-7: Incremental Append
- Support appending new chunks to an existing corpus
- Appended chunks are added to the sorted order by hash
- New root is recomputed; old proofs remain valid for unchanged chunks
- API: `append_chunks(new_chunk_dir: Path) -> NewMerkleCap`

## 4. Data Structures

```rust
// From plonky2/src/hash/merkle_tree.rs
use plonky2::hash::merkle_tree::{MerkleTree, MerkleCap};
use plonky2::hash::hash_types::RichField;

// MerkleProof from plonky2/src/hash/merkle_proofs.rs
use plonky2::hash::merkle_proofs::MerkleProof;
pub struct MerkleProof<F: RichField, H: Hasher<F>> {
    pub siblings: Vec<H::Hash>,  // sibling digests per level
}

// Output JSON structure
struct CorpusMerkleStore {
    cap: Vec<String>,           // 16 PoseidonHash as hex strings
    total_chunks: usize,
    tree_height: usize,
    chunk_hashes: Vec<String>,  // sorted leaf hashes as hex
}
```

## 5. API Surface

```rust
/// Build a new Merkle tree from a directory of chunk files
pub fn build_corpus_merkle_tree(chunks_dir: &Path) -> Result<CorpusMerkleStore, Error>;

/// Get Merkle proof for chunk at a given sorted index
pub fn get_merkle_proof(store: &CorpusMerkleStore, sorted_index: usize) -> MerkleProof;

#[derive(Serialize, Deserialize)]
pub struct CorpusMerkleStore {
    pub cap: Vec<Hash>,        // 16 x PoseidonHash
    pub chunk_hashes: Vec<Hash>,
    pub total_chunks: usize,
}
```

## 6. Edge Cases

| Scenario | Handling |
|----------|----------|
| Empty chunks directory | Return error; no valid root can be produced |
| Single chunk | Tree has 1 leaf; cap has 1 value at height 0, padded to 16 |
| Non-power-of-2 chunk count | Pad with zero-filled leaves to next power of 2 |
| Duplicate chunk content | Each occurrence gets its own leaf; proofs are per-instance |
| File read error | Log error, skip file, continue; fail if 0 chunks loaded |
| Very large corpus (>100K chunks) | Build incrementally; stream leaves to avoid OOM |

### FR-8: Environment and Portability

All paths are driven by environment variables — see **Section 4 (Portability and System Setup)** in the project plan for the full `.env` schema.

| Variable | Required | Description |
|----------|----------|-------------|
| `CHUNKS_DIR` | Yes | Root directory containing chunk files `{doc_id}_{chunk_index}.txt` |
| `ZK_TREE_HEIGHT` | Yes | Merkle tree height (must be known at build time) |
| `ZK_MERKLE_CAP_HEIGHT` | Yes | MerkleCap height (default: 4) |

On a fresh system: see **Section 4.2** of the project plan for the directory layout and setup steps.

## 7. Acceptance Criteria

- [ ] `build_corpus_merkle_tree(&std::env::var("CHUNKS_DIR")?)` returns a valid `CorpusMerkleStore`
- [ ] `cap` field is always exactly 16 PoseidonHash values
- [ ] Rebuilding from the same chunks directory produces bit-for-bit identical output
- [ ] `get_merkle_proof(store, i)` returns a proof that verifies against `store.cap`
- [ ] The MerkleProof is serializable to JSON for use in ZK witness input
- [ ] Unit tests cover: empty, single, power-of-2, non-power-of-2 chunk counts
- [ ] CLI command: `zk-rag build-tree --output corpus_merkle_tree.json` (reads `CHUNKS_DIR` from environment)
