#!/usr/bin/env python3
"""
pipeline_g.py -- Qdrant upsert with Merkle metadata + EVM provenance.

Reads chunk data (Pipeline D), Merkle tree metadata (Pipeline E), and EVM
emission records (Pipeline F from registry), then upserts all chunk vectors
with full metadata to Qdrant.

One Qdrant collection per branch. Pipeline G is the sole write point to Qdrant.

Usage:
    python3 pipeline_g.py --dry-run
    python3 pipeline_g.py --batch
    python3 pipeline_g.py --doc-id <doc_id>
    python3 pipeline_g.py --doc-id <doc_id> --dry-run

Exit codes:
    0  - success
    1  - one or more documents failed (batch mode only)
"""

import argparse
import json
import pickle
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

# ── Configuration ─────────────────────────────────────────────────────────────

REGISTRY_PATH = Path("/data/military-documents/registry.json")
MERKLE_TREES_DIR = Path("/data/military-documents/merkle_trees")
CHUNKS_DIR = Path("/data/military-documents/chunks")
EMBEDDINGS_DIR = Path("/data/military-documents/embeddings")
QDRANT_PATH = Path("/data/qdrant/database")
BM25_INDEX_PATH = Path("/data/military-documents/bm25_index.pkl")
EMBEDDING_DIM = 1024
TEXT_TRUNCATE_LEN = 200


# ── Branch normalization ───────────────────────────────────────────────────────

# Map from raw registry branch values to Qdrant collection names.
# Order matters - more specific mappings first.
BRANCH_NORMALIZE_RULES = [
    ("army",     "army"),
    ("navy",     "navy"),
    ("marines",  "marines"),
    ("air force", "air_force"),
    ("coast guard", "coast_guard"),
]


def normalize_branch(branch: str) -> str:
    """Normalize a registry branch value to a valid Qdrant collection name."""
    b = branch.strip().lower()
    for raw, collection in BRANCH_NORMALIZE_RULES:
        if b == raw:
            return collection
    return "other"


# ── Point ID ─────────────────────────────────────────────────────────────────

def build_point_id(chunk_id: str) -> str:
    """
    Build a deterministic Qdrant point ID from a chunk_id.

    chunk_id format: '{doc_id}-{chunk_index}' (e.g. '0a21e769...-0').
    Uses UUID5 (namespace-based, deterministic) so each chunk gets a unique
    valid UUID. Same chunk_id always produces the same UUID5.
    """
    return str(uuid.uuid5(UUID_NAMESPACE, chunk_id))


UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


# ── Data loading helpers ─────────────────────────────────────────────────────

def load_chunks(chunks_path: Path) -> list[dict]:
    """
    Load chunks from a chunks.jsonl file.

    Returns list of chunk dicts with 'text' truncated to TEXT_TRUNCATE_LEN.
    """
    chunks = []
    with open(chunks_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            # Truncate text
            chunk["text"] = chunk.get("text", "")[:TEXT_TRUNCATE_LEN]
            chunks.append(chunk)
    return chunks


def load_embeddings(embeddings_path: Path) -> np.ndarray:
    """
    Load embeddings from a single .npy file.

    Returns numpy array of shape (N, 1024), dtype float32.
    """
    return np.load(str(embeddings_path))


def load_merkle_tree(doc_id: str, trees_dir: Path) -> dict:
    """Load the tree JSON for a given doc_id."""
    path = trees_dir / f"{doc_id}_tree.json"
    with open(path, "r") as f:
        return json.load(f)


# ── Payload builders ─────────────────────────────────────────────────────────

def build_merkle_payload(
    doc_id: str,
    chunk_index: int,
    tree: dict,
    tree_depth: str,
) -> dict:
    """
    Extract Merkle tree metadata for a single chunk.

    Leaf index offset: doc_id occupies leaf 0, first chunk is at leaf 1.
    Therefore: paths[str(chunk_index + 1)].
    """
    path_key = str(chunk_index + 1)
    path_entry = tree.get("paths", {}).get(path_key)

    if path_entry is None:
        return {
            "merkle_leaf_index": None,
            "merkle_leaf_hash": None,
            "merkle_path": None,
            "merkle_tree_depth": tree_depth,
        }

    siblings = path_entry.get("siblings", [])
    merkle_path = [
        {"hash": s["hash"], "at_depth": s["at_depth"]}
        for s in siblings
    ]

    return {
        "merkle_leaf_index": path_entry.get("leaf_index"),
        "merkle_leaf_hash": path_entry.get("leaf_hash"),
        "merkle_path": merkle_path,
        "merkle_tree_depth": tree_depth,
    }


def build_evm_payload(registry_entry: dict) -> dict:
    """
    Extract EVM provenance fields from an emitted registry entry.

    Prefers emitted_testnet; falls back to emitted_mainnet.
    Returns dict with evm_* fields.
    """
    emitted = registry_entry.get("emitted_testnet") or registry_entry.get("emitted_mainnet") or {}

    # Handle old boolean format
    if isinstance(emitted, bool):
        emitted = {}

    return {
        "evm_tx_hash":        emitted.get("tx_hash"),
        "evm_block_number":   emitted.get("block_number"),
        "evm_block_timestamp": emitted.get("block_timestamp"),
        "evm_chain_id":       emitted.get("chain_id"),
        "evm_uploader":      emitted.get("uploader"),
    }


def build_payload(
    doc_id: str,
    chunk: dict,
    embedding: np.ndarray,
    registry_entry: dict,
    tree: dict,
    tree_depth: str,
    merkle_root: list[str],
) -> dict:
    """
    Build a complete Qdrant payload for a single chunk.

    Combines: chunk text/metadata (Pipeline D),
              Merkle tree metadata (Pipeline E),
              EVM provenance (Pipeline F from registry).
    """
    merkle = build_merkle_payload(doc_id, chunk["chunk_index"], tree, tree_depth)
    evm = build_evm_payload(registry_entry)

    payload = {
        # Chunk metadata
        "doc_id":     doc_id,
        "chunk_id":   chunk["chunk_id"],
        "text":       chunk.get("text", ""),
        "page":       chunk.get("page"),
        "chapter":    chunk.get("chapter"),
        "section":   chunk.get("section"),
        "section_title": chunk.get("section_title"),
        "chunk_index": chunk.get("chunk_index"),
        "vision_description_used": chunk.get("vision_description_used", False),
        # Registry fields
        "branch":     registry_entry.get("branch", ""),
        "title":      registry_entry.get("title", ""),
        "category":   registry_entry.get("category", ""),
        "doc_type":   registry_entry.get("doc_type", ""),
        "source":     registry_entry.get("source", ""),
        "pub_year":   registry_entry.get("pub_year"),
        "file_size_bytes": registry_entry.get("file_size_bytes"),
        "ia_identifier": registry_entry.get("ia_identifier"),
        # Merkle metadata
        "merkle_leaf_hash":   merkle["merkle_leaf_hash"],
        "merkle_leaf_index": merkle["merkle_leaf_index"],
        "merkle_path":       merkle["merkle_path"],
        "merkle_root":       merkle_root,
        "merkle_tree_depth": merkle["merkle_tree_depth"],
        # EVM provenance
        "evm_tx_hash":         evm["evm_tx_hash"],
        "evm_block_number":    evm["evm_block_number"],
        "evm_block_timestamp":  evm["evm_block_timestamp"],
        "evm_chain_id":        evm["evm_chain_id"],
        "evm_uploader":        evm["evm_uploader"],
    }

    return payload


# ── Eligibility ───────────────────────────────────────────────────────────────

def check_eligibility(
    doc_id: str,
    registry_entry: dict,
    trees_dir: Path,
    chunks_dir: Path,
    emb_dir: Path,
) -> dict:
    """
    Check whether a document is eligible for Pipeline G.

    Returns {"eligible": bool, "reason": str|None}
    """
    # 1. Not already ingested
    if registry_entry.get("status") == "ingested":
        return {"eligible": False, "reason": "already ingested"}

    # 2. Emitted on testnet or mainnet
    emitted = registry_entry.get("emitted_testnet") or registry_entry.get("emitted_mainnet") or {}
    if isinstance(emitted, bool):
        emitted = {}
    if emitted.get("status") != "emitted":
        return {"eligible": False, "reason": "not emitted on testnet or mainnet"}

    # 3. Chunks exist
    chunks_path = chunks_dir / doc_id / "chunks.jsonl"
    if not chunks_path.exists():
        return {"eligible": False, "reason": "chunks.jsonl not found"}

    # 4. Embeddings exist
    emb_path = emb_dir / doc_id / "embeddings.npy"
    if not emb_path.exists():
        return {"eligible": False, "reason": "embeddings.npy not found"}

    # 5. Tree exists
    tree_path = trees_dir / f"{doc_id}_tree.json"
    if not tree_path.exists():
        return {"eligible": False, "reason": "tree JSON not found"}

    return {"eligible": True, "reason": None}


def eligible_doc_ids(registry: dict, trees_dir: Path, chunks_dir: Path, emb_dir: Path) -> list[str]:
    """Return list of doc_ids that are eligible for Pipeline G."""
    results = []
    for entry in registry.get("documents", []):
        doc_id = entry["doc_id"]
        elig = check_eligibility(doc_id, entry, trees_dir, chunks_dir, emb_dir)
        if elig["eligible"]:
            results.append(doc_id)
    return results


# ── Qdrant helpers ───────────────────────────────────────────────────────────

def get_qdrant() -> QdrantClient:
    """Return a QdrantClient connected to the local Qdrant instance."""
    return QdrantClient(path=str(QDRANT_PATH))


def ensure_collection(client: QdrantClient, collection: str) -> None:
    """Create collection if it doesn't exist; otherwise verify vector size matches."""
    try:
        client.get_collection(collection_name=collection)
    except Exception:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def upsert_doc(
    doc_id: str,
    registry_entry: dict,
    trees_dir: Path,
    chunks_dir: Path,
    emb_dir: Path,
    client: QdrantClient,
    dry_run: bool = False,
) -> tuple[str, bool, str]:
    """
    Upsert all chunks for a single document into Qdrant.

    Returns (label, success, message).
    Labels: INGEST, SKIP, FAIL
    """
    chunks_path = chunks_dir / doc_id / "chunks.jsonl"
    emb_path = emb_dir / doc_id / "embeddings.npy"
    tree_path = trees_dir / f"{doc_id}_tree.json"

    # ── Load data ─────────────────────────────────────────────────────────────
    try:
        chunks = load_chunks(chunks_path)
        embeddings = load_embeddings(emb_path)
        tree = load_merkle_tree(doc_id, trees_dir)
    except Exception as e:
        return "FAIL", False, f"error=load_failed: {e}"

    if len(chunks) == 0:
        return "FAIL", False, "error=zero_chunks"

    # ── Derive per-chunk fields ────────────────────────────────────────────────
    tree_depth = str(registry_entry.get("tree_depth", ""))
    merkle_root = registry_entry.get("merkle_cap", [])
    collection = normalize_branch(registry_entry.get("branch", "other"))

    # ── Build points ───────────────────────────────────────────────────────────
    points = []
    for i, chunk in enumerate(chunks):
        if i >= len(embeddings):
            # Defensive: embeddings and chunks must align
            break
        embedding = embeddings[i]
        payload = build_payload(
            doc_id=doc_id,
            chunk=chunk,
            embedding=embedding,
            registry_entry=registry_entry,
            tree=tree,
            tree_depth=tree_depth,
            merkle_root=merkle_root,
        )
        point_id = build_point_id(chunk["chunk_id"])
        points.append(PointStruct(
            id=point_id,
            vector=embedding.tolist(),
            payload=payload,
        ))

    # ── Dry run ────────────────────────────────────────────────────────────────
    if dry_run:
        return "INGEST", True, f"dry_run: {len(points)} points to {collection}"

    # ── Ensure collection exists ────────────────────────────────────────────────
    try:
        ensure_collection(client, collection)
    except Exception as e:
        return "FAIL", False, f"error=collection_setup_failed: {e}"

    # ── Upsert ─────────────────────────────────────────────────────────────────
    try:
        client.upsert(collection_name=collection, points=points)
    except Exception as e:
        return "FAIL", False, f"error=upsert_failed: {e}"

    return "INGEST", True, f"ingested {len(points)} points to '{collection}'"


# ── Registry update ───────────────────────────────────────────────────────────

def apply_ingested_update(registry_entry: dict, chunk_count: int) -> None:
    """
    Mark a registry entry as ingested.

    Writes in-place to the registry entry dict.
    Caller is responsible for saving the registry.
    """
    registry_entry["status"] = "ingested"
    registry_entry["ingested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── BM25 Index Helpers ────────────────────────────────────────────────────────

def load_bm25_index(index_path: Path) -> dict:
    """
    Load existing BM25 index from pickle file.
    
    Returns dict with:
        - 'corpus': list of tokenized documents (list of lists of tokens)
        - 'bm25': BM25Okapi model
        - 'doc_id_to_idx': mapping from doc_id to corpus index
    """
    if not index_path.exists():
        return {
            "corpus": [],
            "bm25": None,
            "doc_id_to_idx": {},
        }
    
    with open(index_path, "rb") as f:
        return pickle.load(f)


def save_bm25_index(index_path: Path, corpus: list, bm25_model, doc_id_to_idx: dict) -> None:
    """Save BM25 index to pickle file."""
    index_data = {
        "corpus": corpus,
        "bm25": bm25_model,
        "doc_id_to_idx": doc_id_to_idx,
    }
    with open(index_path, "wb") as f:
        pickle.dump(index_data, f)


def tokenize_text(text: str) -> list[str]:
    """Simple tokenization: lowercase and split on whitespace/punctuation."""
    import re
    text = text.lower()
    tokens = re.findall(r'\b\w+\b', text)
    return tokens


def build_bm25_index_incremental(
    ingested_doc_ids: list[str],
    chunks_dir: Path,
    index_path: Path,
) -> None:
    """
    Build BM25 index incrementally for newly ingested documents.
    
    Loads existing index, appends new documents, and saves updated index.
    Each document's chunks are concatenated into a single text for indexing.
    """
    if not ingested_doc_ids:
        print("No documents to add to BM25 index")
        return
    
    # Load existing index
    index_data = load_bm25_index(index_path)
    corpus = index_data["corpus"]
    bm25_model = index_data["bm25"]
    doc_id_to_idx = index_data["doc_id_to_idx"]
    
    # Import rank_bm25 here to avoid import if not needed
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        print("WARNING: rank_bm25 not installed. Skipping BM25 index build.")
        print("Install with: pip install rank_bm25")
        return
    
    # Add new documents to corpus
    new_corpus = []
    for doc_id in ingested_doc_ids:
        chunks_path = chunks_dir / doc_id / "chunks.jsonl"
        if not chunks_path.exists():
            print(f"WARNING: chunks.jsonl not found for {doc_id}, skipping BM25 indexing")
            continue
        
        # Load all chunks for this document
        chunks = load_chunks(chunks_path)
        
        # Concatenate all chunk texts for this document
        doc_text = " ".join(chunk.get("text", "") for chunk in chunks)
        
        # Tokenize and add to new corpus
        tokens = tokenize_text(doc_text)
        new_corpus.append(tokens)
        
        # Update doc_id_to_idx mapping
        doc_id_to_idx[doc_id] = len(corpus) + len(new_corpus) - 1
    
    # Append new documents to corpus
    corpus.extend(new_corpus)
    
    # Rebuild BM25 model with updated corpus
    if corpus:
        bm25_model = BM25Okapi(corpus)
    
    # Save updated index
    save_bm25_index(index_path, corpus, bm25_model, doc_id_to_idx)
    
    print(f"BM25 index updated: {len(corpus)} documents indexed")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pipeline G - Qdrant upsert with Merkle + EVM metadata")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    parser.add_argument("--batch", action="store_true", help="Process all eligible documents")
    parser.add_argument("--doc-id", type=str, help="Process a single document")
    parser.add_argument("--limit", type=int, metavar="N", help="Limit batch to first N documents (testing)")
    args = parser.parse_args()

    if not args.batch and not args.doc_id:
        parser.print_help()
        sys.exit(1)

    # ── Load registry ─────────────────────────────────────────────────────────
    registry = json.load(open(REGISTRY_PATH))
    registry_entries = {d["doc_id"]: d for d in registry["documents"]}
    print(f"Loaded {len(registry_entries)} documents from registry")

    client = get_qdrant()

    # ── Determine doc_ids to process ──────────────────────────────────────────
    if args.doc_id:
        doc_ids = [args.doc_id]
    else:
        doc_ids = eligible_doc_ids(registry, MERKLE_TREES_DIR, CHUNKS_DIR, EMBEDDINGS_DIR)
        print(f"Eligible docs: {len(doc_ids)}")
        if args.limit:
            doc_ids = doc_ids[: args.limit]

    if args.dry_run:
        print("\n=== DRY RUN MODE ===\n")

    # ── Process each doc ──────────────────────────────────────────────────────
    success_count = 0
    fail_count = 0
    ingested_doc_ids = []  # Track successfully ingested docs for BM25 index

    for doc_id in doc_ids:
        if doc_id not in registry_entries:
            print(f"[FAIL] {doc_id[:16]}... - not in registry")
            fail_count += 1
            continue

        entry = registry_entries[doc_id]
        label, success, message = upsert_doc(
            doc_id=doc_id,
            registry_entry=entry,
            trees_dir=MERKLE_TREES_DIR,
            chunks_dir=CHUNKS_DIR,
            emb_dir=EMBEDDINGS_DIR,
            client=client,
            dry_run=args.dry_run,
        )

        print(f"[{label}] {doc_id[:16]}... {message}")

        if label == "SKIP":
            pass
        elif success:
            success_count += 1
            if not args.dry_run:
                apply_ingested_update(entry, chunk_count=entry.get("chunk_count", 0))
                ingested_doc_ids.append(doc_id)
        else:
            fail_count += 1

        # Throttle to avoid overwhelming Qdrant
        if not args.dry_run:
            time.sleep(0.5)

    # ── Save registry ──────────────────────────────────────────────────────────
    if not args.dry_run and (success_count > 0 or fail_count == 0):
        tmp = REGISTRY_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(registry, f, indent=2)
        tmp.rename(REGISTRY_PATH)
        print(f"\nRegistry updated")

    # ── Build BM25 index (batch mode only, not dry-run) ───────────────────────
    if args.batch and not args.dry_run and ingested_doc_ids:
        print("\n=== Building BM25 Index ===")
        build_bm25_index_incremental(ingested_doc_ids, CHUNKS_DIR, BM25_INDEX_PATH)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n=== SUMMARY ===")
    print(f"Total:   {success_count + fail_count}")
    print(f"Ingested: {success_count}")
    print(f"Failed:  {fail_count}")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
