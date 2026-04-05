#!/usr/bin/env python3
"""Parse critique.md and append rework stories to prd.json."""

import json
import re
import sys


def extract_rework_items(critique_content: str) -> list[tuple[str, str, str]]:
    """Extract Must Rework items as (title, file, suggestion) tuples."""
    items = []
    # Match the Must Rework section
    must_rework_match = re.search(
        r'## _ Must Rework \(high risk or clearly wrong\)\s*\n(.*?)(?=\n## |\Z)',
        critique_content,
        re.DOTALL
    )
    if not must_rework_match:
        return items

    section = must_rework_match.group(1)
    # Split by ### headers to get individual items
    item_pattern = re.compile(r'### (.*?)\n(.*?)(?=\n### |\Z)', re.DOTALL)
    for match in item_pattern.finditer(section):
        title = match.group(1).strip()
        content = match.group(2)
        # Extract File: and Suggestion: lines
        file_match = re.search(r'File:\s*(\S+)', content)
        suggestion_match = re.search(r'Suggestion:\s*(.+)', content)
        if file_match and suggestion_match:
            items.append((title, file_match.group(1), suggestion_match.group(1).strip()))
    return items


def generate_rework_stories(items: list[tuple[str, str, str]], existing_ids: set[int]) -> list[dict]:
    """Generate rework story dicts with auto-numbered IDs."""
    stories = []
    next_num = 1
    for title, target_file, desired_behavior in items:
        while next_num in existing_ids:
            next_num += 1
        story = {
            "id": f"RW-{next_num}",
            "title": title,
            "type": "rework",
            "target_file": target_file,
            "desired_behavior": desired_behavior,
            "preserve": ["existing function signatures", "all acceptance criteria from original story"],
            "acceptance_criteria": [f"python3 -m py_compile {target_file} exits 0"],
            "passes": False,
            "attempts": 0
        }
        stories.append(story)
        next_num += 1
    return stories


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <critique_md_path> <prd_json_path>")
        sys.exit(1)

    critique_path, prd_path = sys.argv[1], sys.argv[2]

    with open(critique_path, 'r', encoding='utf-8') as f:
        critique_content = f.read()

    with open(prd_path, 'r', encoding='utf-8') as f:
        prd_data = json.load(f)

    items = extract_rework_items(critique_content)
    if not items:
        print("No rework stories needed")
        sys.exit(0)

    existing_ids = {int(s['id'].split('-')[1]) for s in prd_data.get('userStories', []) if s['id'].startswith('RW-')}
    new_stories = generate_rework_stories(items, existing_ids)

    if 'userStories' not in prd_data:
        prd_data['userStories'] = []
    prd_data['userStories'].extend(new_stories)

    with open(prd_path, 'w', encoding='utf-8') as f:
        json.dump(prd_data, f, indent=2)

    print(f"Added {len(new_stories)} rework stories to {prd_path}")


if __name__ == '__main__':
    main()
