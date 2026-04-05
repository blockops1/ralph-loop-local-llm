import sys
import json
import argparse
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

def main():
    parser = argparse.ArgumentParser(description='Build Qdrant local index from embeddings + BM25')
    parser.add_argument('--embeddings', required=True, help='Path to embeddings.npy')
    parser.add_argument('--chunk-ids', required=True, help='Path to chunk_ids.json')
    parser.add_argument('--chunks', required=True, help='Path to chunks.jsonl')
    parser.add_argument('--collection', default='rag', help='Qdrant collection name')
    parser.add_argument('--qdrant-path', default='./qdrant_storage', help='Local Qdrant storage path')
    args = parser.parse_args()

    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct

    embeddings = np.load(args.embeddings)
    chunk_ids = json.loads(Path(args.chunk_ids).read_text())

    chunks = {}
    with open(args.chunks) as f:
        for line in f:
            line = line.strip()
            if line:
                c = json.loads(line)
                chunks[c['chunk_id']] = c

    client = QdrantClient(path=args.qdrant_path)
    dim = embeddings.shape[1]

    client.recreate_collection(
        collection_name=args.collection,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
    )

    points = []
    for i, chunk_id in enumerate(chunk_ids):
        chunk = chunks.get(chunk_id, {})
        points.append(PointStruct(
            id=i,
            vector=embeddings[i].tolist(),
            payload={
                'chunk_id': chunk_id,
                'text': chunk.get('text', ''),
                'doc_id': chunk.get('doc_id'),
                'page': chunk.get('page'),
                'chapter': chunk.get('chapter'),
                'section': chunk.get('section'),
                'section_title': chunk.get('section_title'),
                'exhibit': chunk.get('exhibit'),
                'chunk_index': chunk.get('chunk_index'),
            }
        ))

    batch_size = 256
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=args.collection, points=points[i:i+batch_size])

    print(f"Done. {len(points)} vectors indexed into collection '{args.collection}' at {args.qdrant_path}")

if __name__ == '__main__':
    main()
