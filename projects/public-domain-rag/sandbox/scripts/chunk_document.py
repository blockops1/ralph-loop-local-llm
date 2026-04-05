import sys
import json
import uuid
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from langchain_text_splitters import RecursiveCharacterTextSplitter

def main():
    parser = argparse.ArgumentParser(description='Chunk document pages into overlapping text chunks for RAG')
    parser.add_argument('--doc-dir', required=True, help='Directory containing manifest.json and pages/')
    parser.add_argument('--chunk-size', type=int, default=512)
    parser.add_argument('--overlap', type=int, default=100)
    parser.add_argument('--out-dir', required=True, help='Output directory for chunks.jsonl')
    args = parser.parse_args()

    doc_dir = Path(args.doc_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((doc_dir / 'manifest.json').read_text())
    doc_id = manifest['doc_id']

    pages_dir = doc_dir / 'pages'
    page_files = sorted(pages_dir.glob('*.json'), key=lambda p: int(p.stem))
    pages = [json.loads(p.read_text()) for p in page_files]

    full_text = ''
    page_offsets = []
    for page in pages:
        page_offsets.append((len(full_text), page))
        full_text += f"\n[PAGE {page['page']}]\n" + page.get('text', '')

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=args.chunk_size,
        chunk_overlap=args.overlap,
        separators=['\n\n', '\n', ' ']
    )
    chunks_text = splitter.split_text(full_text)

    def find_page(offset):
        for start, page in reversed(page_offsets):
            if offset >= start:
                return page
        return pages[0]

    out_path = out_dir / 'chunks.jsonl'
    count = 0
    with open(out_path, 'w') as f:
        for i, text in enumerate(chunks_text):
            if len(text.strip()) < 50:
                continue
            offset = full_text.find(text)
            page = find_page(offset if offset >= 0 else 0)
            chunk = {
                'chunk_id': str(uuid.uuid4()),
                'doc_id': doc_id,
                'text': text,
                'page': page.get('page'),
                'chapter': page.get('chapter'),
                'section': page.get('section'),
                'section_title': page.get('section_title'),
                'exhibit': (page.get('exhibit_refs') or [None])[0],
                'chunk_index': i
            }
            f.write(json.dumps(chunk) + '\n')
            count += 1

    print(f'Done. {count} chunks written to {out_path}')

if __name__ == '__main__':
    main()
