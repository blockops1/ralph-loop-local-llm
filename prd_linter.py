#!/usr/bin/env python3
"""
prd_linter.py -- Validate a prd.json before Ralph launches.

Runs a set of checks and returns a list of issue dicts:
  [{"level": "ERROR"|"WARN", "story_id": str|None, "message": str}]
"""

from pathlib import Path

# Threshold: warn if story description exceeds this many words
DESC_WORD_LIMIT = 800


def lint_prd(prd: dict, project_dir: str) -> list:
    """
    Lint a prd dict. Returns list of issue dicts.
    Each issue: {"level": "ERROR"|"WARN", "story_id": str|None, "message": str}
    """
    issues = []
    stories = prd.get("userStories", [])

    for story in stories:
        sid = story.get("id", "?")
        desc = story.get("description", "")

        # Check 1: description present and non-trivial
        if len(desc.strip()) < 20:
            issues.append({
                "level": "ERROR",
                "story_id": sid,
                "message": "Description is missing or too short (< 20 chars). Ralph needs a clear spec to work from."
            })

        # Check 2: acceptance criteria present
        ac = story.get("acceptanceCriteria", [])
        if not ac:
            issues.append({
                "level": "ERROR",
                "story_id": sid,
                "message": "No acceptanceCriteria defined. Ralph has no pass/fail signal without them."
            })

        # Check 3: description word count
        word_count = len(desc.split())
        if word_count > DESC_WORD_LIMIT:
            issues.append({
                "level": "WARN",
                "story_id": sid,
                "message": f"Description is {word_count} words (limit {DESC_WORD_LIMIT}). Consider splitting this story."
            })

        # Check 4: contextFiles exist on disk
        for ctx_file in story.get("contextFiles", []):
            if not Path(ctx_file).exists():
                issues.append({
                    "level": "WARN",
                    "story_id": sid,
                    "message": f"contextFiles entry not found on disk: {ctx_file}"
                })

        # Check 5: modifying existing file but no contextFiles listed
        _modify_keywords = ("update", "modify", "extend", "add to", "insert into", "edit")
        desc_lower = desc.lower()
        if any(kw in desc_lower for kw in _modify_keywords) and not story.get("contextFiles"):
            issues.append({
                "level": "WARN",
                "story_id": sid,
                "message": (
                    "Description implies modifying an existing file but contextFiles is empty. "
                    "Add the target file to contextFiles so Ralph reads the right version."
                )
            })

        # Check 6: modifying large file with no anchor hint
        _anchor_keywords = ("line ", "insert after", "insert before", "replace", "anchor", "def ", "class ")
        ctx_files = story.get("contextFiles", [])
        has_large_file = any(Path(f).exists() and Path(f).stat().st_size > 8000 for f in ctx_files)
        has_anchor = any(kw in desc for kw in _anchor_keywords)
        if has_large_file and not has_anchor:
            issues.append({
                "level": "WARN",
                "story_id": sid,
                "message": (
                    "Story modifies a large file (>8KB) but description has no anchor hint "
                    "(line number, function name, or insert-after marker). "
                    "Ralph may rewrite the entire file. Add an anchor to the description."
                )
            })

        # Check 7: qualityChecks present
        qc = story.get("qualityChecks", [])
        if not qc:
            issues.append({
                "level": "WARN",
                "story_id": sid,
                "message": "No qualityChecks defined. Add at least one compile or test check."
            })

    return issues


def format_issues(issues: list) -> str:
    """Format issues list as a human-readable string."""
    if not issues:
        return "PRD lint: OK (no issues)"
    lines = [f"PRD lint: {len(issues)} issue(s)"]
    for i in issues:
        sid = f"[{i['story_id']}] " if i.get("story_id") else ""
        lines.append(f"  {i['level']} {sid}{i['message']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("Usage: prd_linter.py <prd.json>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        prd = json.load(f)
    project_dir = str(Path(sys.argv[1]).parent)
    issues = lint_prd(prd, project_dir)
    print(format_issues(issues))
    sys.exit(1 if any(i["level"] == "ERROR" for i in issues) else 0)
