#!/usr/bin/env python3
"""
sync_review_files.py — Snapshot production files into a Ralph project directory.

Usage:
    python3 scripts/sync_review_files.py base-trader-audit
    python3 scripts/sync_review_files.py base-trader-audit --check  # dry run

Copies the contextFiles from prd.json into:
    ralph/projects/<slug>/code/
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

RALPH_DIR = Path(__file__).parent.parent
WORKSPACE = Path(__file__).parent.parent.parent.resolve()  # /Users/jill/.hermes/workspace


def sync_project(slug: str, dry_run: bool = False) -> None:
    prd_path = RALPH_DIR / "projects" / slug / "prd.json"
    if not prd_path.exists():
        print(f"Error: prd.json not found at {prd_path}")
        sys.exit(1)

    import json
    prd = json.loads(prd_path.read_text())

    # Find contextFiles — could be in userStories[0].contextFiles or top-level
    context_files = []
    for story in prd.get("userStories", []):
        if story.get("type") == "review" or "review" in story.get("id", "").lower():
            context_files = story.get("contextFiles", [])
            break
    if not context_files:
        # Fallback: top-level or first story
        context_files = prd.get("contextFiles", prd.get("userStories", [{}])[0].get("contextFiles", []))

    code_dir = RALPH_DIR / "projects" / slug / "code"
    code_dir.mkdir(exist_ok=True)

    copied = []
    for rel_path in context_files:
        src = WORKSPACE / rel_path
        dst = code_dir / rel_path.replace("/", "_")
        if not src.exists():
            print(f"[SYNC] WARNING: {src} not found — skipping")
            continue
        if dry_run:
            print(f"[SYNC] Would copy: {src} -> {dst}")
        else:
            shutil.copy2(src, dst)
            copied.append(str(dst))
            print(f"[SYNC] Copied: {rel_path} -> {dst.relative_to(RALPH_DIR)}")

    manifest_path = code_dir / "_manifest.txt"
    snapshot_marker = code_dir / "_snapshot.txt"
    if not dry_run:
        manifest_path.write_text("\n".join(context_files) + "\n")
        snapshot_marker.write_text(f"Snapshot taken: {datetime.now().isoformat()}\n")
        print(f"[SYNC] Manifest written: {manifest_path.relative_to(RALPH_DIR)}")

        # Patch PRD so Ralph reads from snapshot instead of production files
        import json
        patched = False
        for story in prd.get("userStories", []):
            if story.get("type") == "review" or "review" in story.get("id", "").lower():
                story["contextFiles"] = [
                    f"ralph/projects/{slug}/code/{cf.replace('/', '_')}"
                    for cf in context_files
                ]
                patched = True
        if patched:
            prd_path.write_text(json.dumps(prd, indent=2))
            print(f"[SYNC] PRD updated — contextFiles now pointing to snapshot")
        else:
            print(f"[SYNC] No review story found in PRD — PRD not patched")
    else:
        print(f"[SYNC] Would write manifest: {manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="Project slug (e.g. base-trader-audit)")
    parser.add_argument("--check", action="store_true", help="Dry run only")
    args = parser.parse_args()
    sync_project(args.slug, dry_run=args.check)
