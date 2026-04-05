#!/usr/bin/env python3
"""
check_status.py — Report Ralph project status
Usage: python3 check_status.py [project-slug]
If no slug given, shows all projects.
"""
import json
import sys
from pathlib import Path

RALPH_DIR = Path(__file__).parents[3] / "ralph" / "projects"


def status_for_project(slug):
    prd_path = RALPH_DIR / slug / "prd.json"
    progress_path = RALPH_DIR / slug / "progress.txt"
    if not prd_path.exists():
        return f"{slug}: prd.json not found"

    prd = json.loads(prd_path.read_text())
    stories = prd.get("userStories", [])

    total = len(stories)
    passed = sum(1 for s in stories if s.get("passes"))
    failed = [s for s in stories if s.get("attempts", 0) >= 3 and not s.get("passes")]
    pending = [s for s in stories if not s.get("passes") and s.get("attempts", 0) < 3]

    lines = [f"=== {slug} ==="]
    lines.append(f"Progress: {passed}/{total} stories complete")

    for s in stories:
        icon = "✅" if s.get("passes") else ("🔴" if s.get("attempts", 0) >= 3 else "⏳")
        deps = s.get("dependsOn", [])
        dep_str = f" [deps: {', '.join(deps)}]" if deps else ""
        lines.append(f"  {icon} {s['id']}: {s['title']} (attempts: {s.get('attempts', 0)}){dep_str}")
        if s.get("error"):
            lines.append(f"       Error: {s['error'][:120]}")

    if progress_path.exists():
        lines.append("\nRecent progress:")
        recent = progress_path.read_text().splitlines()[-10:]
        lines.extend(f"  {l}" for l in recent)

    return "\n".join(lines)


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if slug:
        print(status_for_project(slug))
    else:
        projects = [
            p.name
            for p in RALPH_DIR.iterdir()
            if p.is_dir() and (p / "prd.json").exists()
        ]
        if not projects:
            print("No Ralph projects found.")
        else:
            for p in sorted(projects):
                print(status_for_project(p))
                print()
