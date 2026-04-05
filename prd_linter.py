#!/usr/bin/env python3
"""
prd_linter.py - PRD story validator.

Validates that stories have required fields based on their type.
For type:rework stories, validates target_file, output_file, and preserve fields.

Usage:
    python3 prd_linter.py <path-to-prd.json>
    Exits 0 if valid, 1 if invalid.
"""

import json
import sys
from pathlib import Path


def is_shell_ac(ac_string: str) -> bool:
    """
    Check if an acceptance criterion is a shell command.
    Returns True if the AC contains at least one shell command indicator.
    """
    shell_indicators = ['grep', 'python3', 'bash', 'test ', 'ls ', 'cat ', 'wc ', 'exit', '&&', '||', '|', '$(', 'git ']
    return any(indicator in ac_string for indicator in shell_indicators)


def validate_rework_story(story: dict, errors: list) -> None:
    """Validate required fields for a rework-type story."""
    story_id = story.get("id", "unknown")
    
    # target_file is required
    if "target_file" not in story:
        errors.append(f"Story {story_id}: missing required field 'target_file'")
    elif not isinstance(story["target_file"], str) or not story["target_file"].strip():
        errors.append(f"Story {story_id}: 'target_file' must be a non-empty string")
    
    # output_file is required
    if "output_file" not in story:
        errors.append(f"Story {story_id}: missing required field 'output_file'")
    elif not isinstance(story["output_file"], str) or not story["output_file"].strip():
        errors.append(f"Story {story_id}: 'output_file' must be a non-empty string")
    
    # preserve is required and must be a non-empty list
    if "preserve" not in story:
        errors.append(f"Story {story_id}: missing required field 'preserve'")
    elif not isinstance(story["preserve"], list):
        errors.append(f"Story {story_id}: 'preserve' must be a list")
    elif len(story["preserve"]) == 0:
        errors.append(f"Story {story_id}: 'preserve' must contain at least one behavior")
    
    # current_behavior and desired_behavior are optional strings
    if "current_behavior" in story:
        if not isinstance(story["current_behavior"], str):
            errors.append(f"Story {story_id}: 'current_behavior' must be a string (optional)")
    
    if "desired_behavior" in story:
        if not isinstance(story["desired_behavior"], str):
            errors.append(f"Story {story_id}: 'desired_behavior' must be a string (optional)")


def validate_story(story: dict) -> list:
    """Validate a single story. Returns list of error strings."""
    errors = []
    story_id = story.get("id", "unknown")
    story_type = story.get("type", "create")  # default to 'create' if not specified
    
    if story_type == "rework":
        validate_rework_story(story, errors)
    
    # acceptance_criteria is required for all stories (accept both camelCase and snake_case for compatibility)
    if "acceptanceCriteria" in story:
        story["acceptance_criteria"] = story.pop("acceptanceCriteria")
    if "acceptance_criteria" not in story:
        errors.append(f"Story {story_id}: missing required field 'acceptance_criteria'")
    elif not isinstance(story["acceptance_criteria"], list):
        errors.append(f"Story {story_id}: 'acceptance_criteria' must be a list")
    else:
        # Check each AC is a shell command, not prose
        for ac in story["acceptance_criteria"]:
            if not is_shell_ac(ac):
                errors.append(f"Story {story_id} AC is prose, not a shell command: {ac[:60]}")
    
    return errors


def validate_prd(prd_path: str) -> tuple[bool, list]:
    """
    Validate a PRD file.
    Returns (is_valid, errors_list).
    """
    path = Path(prd_path)
    if not path.exists():
        return False, [f"File not found: {prd_path}"]
    
    try:
        with open(path) as f:
            prd = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON: {e}"]
    
    if "userStories" not in prd:
        return False, ["PRD missing 'userStories' field"]
    
    if not isinstance(prd["userStories"], list):
        return False, ["'userStories' must be a list"]
    
    all_errors = []
    for story in prd["userStories"]:
        if "id" not in story:
            all_errors.append("Story missing 'id' field")
            continue
        errors = validate_story(story)
        all_errors.extend(errors)
    
    return len(all_errors) == 0, all_errors


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 prd_linter.py <path-to-prd.json>", file=sys.stderr)
        sys.exit(1)
    
    prd_path = sys.argv[1]
    is_valid, errors = validate_prd(prd_path)
    
    if is_valid:
        print(f"OK: {prd_path} is valid")
        sys.exit(0)
    else:
        print(f"INVALID: {prd_path}", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
