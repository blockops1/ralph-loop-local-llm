import sys
import json
import argparse
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

def main():
    parser = argparse.ArgumentParser(description='Smoke test: embed a query and retrieve top-k chunks from Qdrant')
    parser.add_argument('--query', required=True, help='Query string to test')
    parser.add_argument('--collection', default='rag', help='Qdrant collection name')
    parser.add_argument('--qdrant-path', default='./qdrant_storage', help='Local Qdrant storage path')
    parser.add_argument('--model', default='all-MiniLM-L6-v2', help='Sentence-transformers model name')
    parser.add_argument('--top-k', type=int, default=5, help='Number of results to return')
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer
    from qdrant_client import QdrantClient

    model = SentenceTransformer(args.model)
    query_vec = model.encode([args.query], convert_to_numpy=True)[0].tolist()

    client = QdrantClient(path=args.qdrant_path)
    results = client.search(
        collection_name=args.collection,
        query_vector=query_vec,
        limit=args.top_k,
        with_payload=True
    )

    print(f"Top {args.top_k} results for: '{args.query}'\n")
    for i, r in enumerate(results):
        p = r.payload
        print(f"[{i+1}] score={r.score:.4f} | doc={p.get('doc_id')} page={p.get('page')} chunk={p.get('chunk_index')}")
        print(f"     {p.get('text','')[:200]}")
        print()

if __name__ == '__main__':
    main()