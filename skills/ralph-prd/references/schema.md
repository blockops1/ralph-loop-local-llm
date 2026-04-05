# prd.json Schema Reference

Complete field reference for Ralph PRDs.

---

## Top-Level Fields

```json
{
  "project": "base-trader",
  "slug": "base-trader",
  "branchName": "ralph/base-trader-signal-pipeline",
  "description": "One paragraph: what this PRD builds and why.",
  "contextFiles": ["path/to/shared/context.md"],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "userStories": []
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `project` | ✅ | Display name |
| `slug` | ✅ | Must match directory name in `ralph/projects/` |
| `branchName` | ✅ | Git branch Ralph commits to. Use `ralph/<slug>-<what>` |
| `description` | ✅ | Plain English summary of what this PRD builds |
| `contextFiles` | optional | Files Ralph reads at the start of EVERY story |
| `qualityChecks` | optional | Default quality checks applied to all stories (overridden per-story) |
| `userStories` | ✅ | Array of story objects (see below) |

---

## Story Fields

```json
{
  "id": "US-003",
  "title": "Build scanner.py — Nansen API wrapper with mock support",
  "description": "Current state: ...\n\nWhat to build: ...\n\nConstraints: ...",
  "acceptanceCriteria": [
    "python3 -m py_compile scripts/scanner.py",
    "NANSEN_MOCK=1 python3 tests/test_scanner.py"
  ],
  "priority": 3,
  "passes": false,
  "attempts": 0,
  "lastAttempt": null,
  "error": null,
  "notes": "",
  "dependsOn": ["US-001"],
  "dependencyPolicy": "block",
  "contextFiles": ["scripts/existing_file.py"],
  "qualityChecks": ["python3 -m py_compile {file}"]
}
```

### Field Reference

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | ✅ | Format: `US-001`, `US-002`, etc. |
| `title` | string | ✅ | One line. Include the filename if creating a file. |
| `description` | string | ✅ | See structure below |
| `acceptanceCriteria` | array | ✅ | Shell commands only — must exit 0 to pass |
| `priority` | int | ✅ | Lower = runs first. Must match execution order. |
| `passes` | bool | ✅ | Set `true` if already done. Ralph skips these. |
| `attempts` | int | ✅ | Start at 0. Ralph increments each try. |
| `lastAttempt` | string/null | optional | ISO timestamp, set by Ralph |
| `error` | string/null | optional | Last failure message, set by Ralph |
| `notes` | string | optional | Ralph appends its own notes here on completion |
| `dependsOn` | array | ✅ | List of story IDs this story requires. `[]` if none. |
| `dependencyPolicy` | string | ✅ | Always `"block"` — skip story if dependency failed |
| `contextFiles` | array | optional | Files Ralph reads before starting this story |
| `qualityChecks` | array | optional | Overrides top-level qualityChecks for this story |

---

## Description Structure (follow this template)

```
Current state: <what exists now — files, functions, state>

What to build: <precise description of what to create or change>
  - Function signatures with types
  - Return formats with examples
  - Behavior for edge cases

Constraints:
  - Do NOT modify X
  - Do NOT add Y
  - Only use stdlib — no new dependencies
```

**The "Current state" section is mandatory.** Without it, Ralph doesn't know if it's creating or modifying.

---

## Pre-Done Story (passes: true)

When work was already done before Ralph runs:

```json
{
  "id": "US-001",
  "title": "Create project directory structure",
  "description": "Directory structure already created in commit e2d9883.",
  "acceptanceCriteria": [
    "test -d businesses/base-trader/scripts",
    "python3 -c \"import json; json.load(open('config/tokens.json'))\""
  ],
  "priority": 1,
  "passes": true,
  "attempts": 0,
  "notes": "Verified: all directories exist, tokens.json valid JSON with core and smart_money keys.",
  "dependsOn": [],
  "dependencyPolicy": "block"
}
```

---

## Resetting a Failed Story

When a story has failed and you want to retry after fixing the PRD:

```json
{
  "passes": false,
  "attempts": 0,
  "error": null,
  "notes": "Reset after adding contextFiles and splitting scope."
}
```

---

## TDD Example: Test Story + Implementation Story

For files with non-trivial logic, split into two stories. See `references/story-rules.md` Rule 13 for the full pattern.

```json
{
  "project": "my-project",
  "slug": "my-project",
  "branchName": "ralph/my-project-feature",
  "description": "Build my-project: config setup and core logic.",
  "contextFiles": ["PROJ-my-project.md"],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "userStories": [
    {
      "id": "US-001",
      "title": "Write tests/test_scanner.py",
      "type": "create",
      "description": "Current state: tests/test_scanner.py does not exist.\n\nCreate the test file with pytest.\nUse MOCK_MODE and patch network calls so tests run without API keys.\nTest: get_price_ohlcv returns dict with keys open/high/low/close/volume.\nTest: error handling when API returns non-200.\n\nConstraints:\n- Do NOT write the implementation — only the test file\n- Use pytest with unittest.mock for patching",
      "target_file": "tests/test_scanner.py",
      "output_file": "tests/test_scanner.py",
      "preserve": [],
      "acceptanceCriteria": [
        "python3 -m py_compile tests/test_scanner.py",
        "MOCK_MODE=1 python3 -m pytest tests/test_scanner.py -v"
      ],
      "qualityChecks": ["python3 -m py_compile tests/test_scanner.py"],
      "priority": 1,
      "passes": false,
      "attempts": 0,
      "dependsOn": [],
      "dependencyPolicy": "block",
      "contextFiles": ["scripts/scanner.py"]
    },
    {
      "id": "US-002",
      "title": "Build scripts/scanner.py",
      "type": "create",
      "description": "Current state: scripts/scanner.py does not exist.\n\nBuild the scanner module. All functions must pass the tests in tests/test_scanner.py (US-001).\nUse MOCK_MODE for all external API calls.\n\nConstraints:\n- Do NOT write the test file — tests already exist in tests/test_scanner.py\n- Implementation must make tests in US-001 pass",
      "target_file": "scripts/scanner.py",
      "output_file": "scripts/scanner.py",
      "preserve": [],
      "acceptanceCriteria": [
        "python3 -m py_compile scripts/scanner.py",
        "MOCK_MODE=1 python3 -m pytest tests/test_scanner.py -v"
      ],
      "qualityChecks": ["python3 -m py_compile scripts/scanner.py"],
      "priority": 2,
      "passes": false,
      "attempts": 0,
      "dependsOn": ["US-001"],
      "dependencyPolicy": "block",
      "contextFiles": ["scripts/scanner.py"]
    }
  ]
}
```

## Full Minimal Example

```json
{
  "project": "my-project",
  "slug": "my-project",
  "branchName": "ralph/my-project-phase1",
  "description": "Build phase 1 of my-project: config setup and core logic.",
  "contextFiles": ["PROJ-my-project.md"],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "userStories": [
    {
      "id": "US-001",
      "title": "Create config/settings.json",
      "description": "Current state: config/ directory does not exist.\n\nCreate config/settings.json with the following structure:\n{\n  'threshold': 175,\n  'max_positions': 2\n}\n\nConstraints: Do not create any other files.",
      "acceptanceCriteria": [
        "test -f config/settings.json",
        "python3 -c \"import json; json.load(open('config/settings.json'))\""
      ],
      "priority": 1,
      "passes": false,
      "attempts": 0,
      "dependsOn": [],
      "dependencyPolicy": "block",
      "contextFiles": []
    }
  ]
}
```
