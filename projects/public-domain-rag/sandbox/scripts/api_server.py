import sys
import json
import argparse
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

def main():
    parser = argparse.ArgumentParser(description='FastAPI RAG query server')
    parser.add_argument('--qdrant-path', default='./qdrant_storage', help='Local Qdrant storage path')
    parser.add_argument('--collection', default='rag', help='Qdrant collection name')
    parser.add_argument('--model', default='all-MiniLM-L6-v2', help='Sentence-transformers model name')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind')
    parser.add_argument('--port', type=int, default=8100, help='Port to listen on')
    parser.add_argument('--top-k', type=int, default=5, help='Default number of results')
    args = parser.parse_args()

    from fastapi import FastAPI
    from pydantic import BaseModel
    from sentence_transformers import SentenceTransformer
    from qdrant_client import QdrantClient
    import uvicorn

    app = FastAPI(title="RAG Query API", version="1.0.0")
    model = SentenceTransformer(args.model)
    client = QdrantClient(path=args.qdrant_path)

    class QueryRequest(BaseModel):
        query: str
        top_k: int = args.top_k
        collection: str = args.collection

    class ChunkResult(BaseModel):
        chunk_id: str
        score: float
        text: str
        doc_id: str | None
        page: int | None
        chapter: str | None
        section: str | None
        section_title: str | None
        exhibit: str | None
        chunk_index: int | None

    @app.post("/query", response_model=list[ChunkResult])
    def query(req: QueryRequest):
        vec = model.encode([req.query], convert_to_numpy=True)[0].tolist()
        results = client.search(
            collection_name=req.collection,
            query_vector=vec,
            limit=req.top_k,
            with_payload=True
        )
        return [
            ChunkResult(
                chunk_id=r.payload.get('chunk_id', ''),
                score=r.score,
                text=r.payload.get('text', ''),
                doc_id=r.payload.get('doc_id'),
                page=r.payload.get('page'),
                chapter=r.payload.get('chapter'),
                section=r.payload.get('section'),
                section_title=r.payload.get('section_title'),
                exhibit=r.payload.get('exhibit'),
                chunk_index=r.payload.get('chunk_index'),
            )
            for r in results
        ]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == '__main__':
    main()
