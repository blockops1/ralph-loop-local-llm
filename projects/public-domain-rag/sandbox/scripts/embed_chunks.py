import sys
import json
import argparse
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

def main():
    parser = argparse.ArgumentParser(description='Embed chunks JSONL to numpy embeddings using sentence-transformers')
    parser.add_argument('--chunks', required=True, help='Path to chunks.jsonl')
    parser.add_argument('--model', default='all-MiniLM-L6-v2', help='Sentence-transformers model name')
    parser.add_argument('--out-dir', required=True, help='Output directory for embeddings.npy and chunk_ids.json')
    parser.add_argument('--batch-size', type=int, default=64, help='Batch size for encoding')
    args = parser.parse_args()

    chunks_path = Path(args.chunks)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    with open(chunks_path) as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))

    if not chunks:
        print("No chunks found — nothing to embed.")
        return

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model)

    texts = [c['text'] for c in chunks]
    chunk_ids = [c['chunk_id'] for c in chunks]

    print(f"Embedding {len(texts)} chunks with model '{args.model}'...")
    embeddings = model.encode(texts, batch_size=args.batch_size, show_progress_bar=True, convert_to_numpy=True)

    emb_path = out_dir / 'embeddings.npy'
    ids_path = out_dir / 'chunk_ids.json'

    np.save(str(emb_path), embeddings)
    ids_path.write_text(json.dumps(chunk_ids, indent=2))

    print(f"Done. {len(chunk_ids)} embeddings saved to {emb_path}")
    print(f"Chunk IDs saved to {ids_path}")
    print(f"Embedding shape: {embeddings.shape}")

if __name__ == '__main__':
    main()
