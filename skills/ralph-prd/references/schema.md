# PRD Schema Reference

Complete reference for `prd.json` fields.

---

## Root Schema

```json
{
  "project": "string",           // Project name
  "slug": "string",               // Must match directory name under projects/
  "branchName": "string",         // Git branch to work on (e.g. "ralph/my-feature")
  "description": "string",        // What this PRD builds and why
  "contextFiles": ["string"],     // Files Ralph should read before starting
  "qualityChecks": ["string"],    // Global quality checks (run after each story)
  "userStories": [ ... ]          // Array of story objects — MUST be "userStories"
}
```

**Critical:** The root array field must be `"userStories"`, not `"stories"`. The pipeline reads `userStories`.

---

## Story Object

### Fields for 4-Stage Pipeline

| Field | Type | Required | Description |
|-------|------|---------|-------------|
| `type` | string | Yes | Must be `"create"` for 4-stage pipeline |
| `target_file` | string | Yes | Path Ralph will write to (container path, e.g. `/app/projects/slug/file.py`) |
| `output_file` | string | Yes | Same as `target_file` for create stories |
| `preserve` | array | Recommended | Behaviors to keep unchanged (strings describing what not to break) |

### Required Fields (all stories)

| Field | Type | Required | Description |
|-------|------|---------|-------------|
| `id` | string | Yes | Unique ID — format `US-001`, `US-002`, etc. |
| `title` | string | Yes | One line — include filename if creating a file |
| `description` | string | Yes | Must include `Current state:` section |
| `contextFiles` | array | Yes | Files Ralph needs to read (existing files being modified must be here) |
| `acceptanceCriteria` | array | Yes | Shell commands — exit 0 = pass, non-zero = fail |
| `qualityChecks` | array | Yes | At least one check |
| `priority` | integer | Yes | Lower number = runs first |
| `passes` | boolean | Yes | Start as `false` |
| `attempts` | integer | Yes | Start at `0` |
| `dependsOn` | array | Yes | Array of story IDs — `[]` if none |
| `dependencyPolicy` | string | Yes | Always `"block"` |

---

## Example: Minimal Create Story

```json
{
  "id": "US-001",
  "title": "Create config.py",
  "type": "create",
  "description": "Current state: No config module exists.\n\nWhat to build: A config.py module that loads settings from environment variables with sensible defaults.\n\nConstraints: Must work with Python 3.11+.",
  "target_file": "/app/projects/my-project/config.py",
  "output_file": "/app/projects/my-project/config.py",
  "preserve": [],
  "contextFiles": [],
  "acceptanceCriteria": [
    "python3 -m py_compile /app/projects/my-project/config.py",
    "python3 -c \"from config import settings; assert 'host' in settings\""
  ],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "priority": 1,
  "passes": false,
  "attempts": 0,
  "dependsOn": [],
  "dependencyPolicy": "block"
}
```

---

## Example: Modify Existing File Story

```json
{
  "id": "US-002",
  "title": "Add retry logic to config.py",
  "type": "create",
  "description": "Current state: config.py loads settings but has no retry logic on network failures.\n\nWhat to build: Add a `with_retry` decorator that retries network calls up to 3 times with exponential backoff.\n\nConstraints: Preserve existing `settings` object and `load()` function signature.",
  "target_file": "/app/projects/my-project/config.py",
  "output_file": "/app/projects/my-project/config.py",
  "preserve": ["settings object unchanged", "load() function signature unchanged"],
  "contextFiles": ["/app/projects/my-project/config.py"],
  "acceptanceCriteria": [
    "python3 -c \"from config import with_retry; print('ok')\"",
    "python3 -m py_compile /app/projects/my-project/config.py"
  ],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "priority": 2,
  "passes": false,
  "attempts": 0,
  "dependsOn": ["US-001"],
  "dependencyPolicy": "block"
}
```

---

## Example: TDD Story Chain

```json
{
  "id": "US-003",
  "title": "Write tests for parser.py",
  "type": "create",
  "description": "Current state: parser.py does not exist yet.\n\nWhat to build: Write test_parser.py with pytest tests for the parser module. Tests should fail gracefully until parser.py is implemented.\n\nConstraints: Use pytest. Test names should be descriptive.",
  "target_file": "/app/projects/my-project/tests/test_parser.py",
  "output_file": "/app/projects/my-project/tests/test_parser.py",
  "preserve": [],
  "contextFiles": [],
  "acceptanceCriteria": [
    "cd /app/projects/my-project && python3 -m pytest tests/test_parser.py -v --tb=short 2>&1 | grep -E '(FAILED|PASSED|ERROR)'"
  ],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "priority": 3,
  "passes": false,
  "attempts": 0,
  "dependsOn": [],
  "dependencyPolicy": "block"
},
{
  "id": "US-004",
  "title": "Create parser.py",
  "type": "create",
  "description": "Current state: parser.py does not exist. tests/test_parser.py has failing tests.\n\nWhat to build: parser.py with a `parse_config(raw: str) -> dict` function that handles JSON and INI formats.\n\nConstraints: Must pass all tests in tests/test_parser.py.",
  "target_file": "/app/projects/my-project/parser.py",
  "output_file": "/app/projects/my-project/parser.py",
  "preserve": [],
  "contextFiles": ["/app/projects/my-project/tests/test_parser.py"],
  "acceptanceCriteria": [
    "cd /app/projects/my-project && python3 -m pytest tests/test_parser.py -v --tb=short",
    "python3 -m py_compile /app/projects/my-project/parser.py"
  ],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "priority": 4,
  "passes": false,
  "attempts": 0,
  "dependsOn": ["US-003"],
  "dependencyPolicy": "block"
}
```

---

## Common Mistakes

| Mistake | Why It's Wrong | Fix |
|---------|---------------|-----|
| `"stories": [...]` instead of `"userStories": [...]` | Pipeline looks for `userStories` | Rename to `"userStories"` |
| `qualityChecks: "python3 -m py_compile"` (string) | Gets iterated character-by-character | Use list: `["python3 -m py_compile {file}"]` |
| No `Current state:` in description | Ralph doesn't know what exists | Add `Current state:` section |
| Existing file not in `contextFiles` | Ralph may create new instead of editing | Add existing file to `contextFiles` |
| Story too large (>150 lines) | Most common failure cause | Split into 2-3 stories |
| AC is prose instead of command | Can't be validated automatically | Replace with shell command |
| Em-dashes or Unicode in text | Causes SyntaxError in Python | Use ASCII hyphens only |
