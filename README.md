# Ralph Loop — Autonomous Coding with Local LLMs

Ralph is an autonomous coding loop that runs a local LLM through structured coding tasks defined in `prd.json`. Each task goes through four isolated stages — build, critique, fix, quality — with no memory between stages.

**Ralph runs in Docker** so it can only write to mounted volumes. It uses a local LLM (Qwen 3.6 35B via llama.cpp is the tested configuration, but any OpenAI-compatible server works).

---

## Architecture

```
Host machine
  ├── llama-server :8090          ← LLM backend (any OpenAI-compatible server)
  └── ~/ralph/                    ← source + config + projects
        ├── docker-compose.yml
        ├── Dockerfile
        └── pipeline_runner.py    ← primary entrypoint

Container (Ralph)
  ├── /app/projects/              ← PRDs + generated code (volume mounted)
  ├── /app/logs/                  ← pipeline logs (volume mounted)
  └── /app/config.yaml            ← model URL
```

**Container networking:** Uses host networking (`network_mode: host`) so `localhost:8090` inside the container reaches the host's llama-server.

---

## Prerequisites

1. **llama-server running on host at `http://localhost:8090`**

   Any OpenAI-compatible inference server works. Tested with llama.cpp:
   ```bash
   brew install llama.cpp
   llama-server --model /path/to/model.gguf --port 8090 --ctx-size 131072 --ngl 99
   ```

   Verify:
   ```bash
   curl -s http://localhost:8090/v1/models | jq
   ```

2. **Docker Desktop** running

3. **Build the image (one time):**
   ```bash
   cd ~/ralph
   docker compose build
   ```

4. **Bot tokens** (optional — for notifications):
   - `DISCORD_BOT_TOKEN` and/or `TELEGRAM_BOT_TOKEN` in `~/.hermes/.env`

---

## Quick Start

```bash
cd ~/ralph

# Run full pipeline on a project
docker compose run --rm ralph my-project-slug

# Run a single story
docker compose run --rm ralph my-project-slug --story US-001

# List all projects
docker compose run --rm ralph python3 pipeline_runner.py --list-projects
```

Ralph sends a Discord/Telegram notification when each story completes and when the pipeline finishes.

---

## Project Directory

Each project lives at `{RALPH_DIR}/projects/{slug}/`:

```
{slug}/
├── prd.json              ← source of truth (must exist)
├── progress.txt          ← append-only run log
├── critique.md           ← stage 2 output (per project)
├── code/                 ← generated code
└── .pipeline.lock        ← concurrency lock (remove if stale)
```

---

## The 4-Stage Pipeline

Every story runs through four completely isolated stages. The creator never reviews its own work — each stage is a fresh model call with no memory of the previous one.

```
Story from prd.json
        │
        ├─► STAGE 1 — CREATE
        │   Ralph builds the file using PROMPT.md
        │   Writes to /app/projects/{slug}/
        │   Git commit
        │
        ├─► STAGE 2 — CRITIQUE
        │   Raw model call (no tools) using PROMPT-critique.md
        │   Reads the output file cold — no memory of how it was built
        │   Writes critique.md next to the output
        │
        ├─► STAGE 3 — FIX
        │   Ralph reworks the file using PROMPT-rework.md
        │   Overwrites the output file
        │   Git commit
        │
        └─► STAGE 4 — QUALITY
            Ruff lint/format -> Pyright type check -> Desloppify scan
            Rework stories auto-injected into prd.json

prd.json updated -> next story
```

**FIX always runs** — even if CRITIQUE passes. It validates and catches issues critique missed.

**QUALITY always runs** — it never fails the pipeline (best-effort). It generates rework stories for mechanical issues and injects them into the PRD.

---

## Writing a PRD

### Core Rule: One Story = One Idea

Bundling multiple concepts into a single story is the most common cause of pipeline failures. When a story contains more than one idea:
- Acceptance criteria become ambiguous
- A single failure blocks unrelated features
- Critique cannot evaluate it properly
- Partial rollbacks become impossible

**Five 50-line stories beat one 250-line story every time.**

### Signs a Story Is Too Large

- Description exceeds ~150 lines
- Contains "also..." or "while we're at it..."
- Multiple `target_file` or `output_file` entries
- Acceptance criteria list is long or compound

### What a Well-Scoped Story Looks Like

- Description fits in 50–100 lines
- One clear goal: "add X to Y" or "fix Z behavior"
- Acceptance criteria are specific shell commands (exit 0 = pass)
- File touched is obvious

---

## PRD Schema

```json
{
  "project": "my-project",
  "slug": "my-project",
  "description": "What this PRD builds and why.",
  "contextFiles": ["EXISTING_FILE.py"],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "userStories": [
    {
      "id": "US-001",
      "title": "One-line title — include filename if creating",
      "type": "create",
      "description": "Current state: ...\n\nWhat to build: ...\n\nConstraints: ...",
      "target_file": "/app/projects/my-project/output.py",
      "output_file": "/app/projects/my-project/output.py",
      "preserve": ["behavior not to change"],
      "acceptanceCriteria": [
        "python3 -m py_compile /app/projects/my-project/output.py"
      ],
      "qualityChecks": ["python3 -m py_compile {file}"],
      "priority": 1,
      "passes": false,
      "attempts": 0,
      "dependsOn": [],
      "dependencyPolicy": "block",
      "contextFiles": ["/app/projects/my-project/code/existing_file.py"]
    }
  ]
}
```

**Critical path rules:**
- All paths in `prd.json` must be **container absolute paths** starting with `/app/projects/`
- Field must be `"userStories"` not `"stories"`
- `type` must be `"create"` for pipeline stories
- `qualityChecks` must be a list, not a string (strings get iterated character-by-character)

---

## Story Fields Reference

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Format: `US-001`, `US-002`, etc. |
| `title` | Yes | One line with filename |
| `type` | Yes | Must be `"create"` for 4-stage pipeline |
| `description` | Yes | Must include `Current state:` section |
| `target_file` | Yes | Path Ralph will write to — no `/code/` subdir |
| `output_file` | Yes | Same as `target_file` for create stories |
| `preserve` | Recommended | Behaviors to keep unchanged |
| `contextFiles` | Yes | Primary output file should be first |
| `acceptanceCriteria` | Yes | Shell commands only — exit 0 = pass |
| `qualityChecks` | Yes | At least one command |
| `priority` | Yes | Lower = runs first |
| `passes` | Yes | Start as `false` |
| `attempts` | Yes | Start at `0` |
| `dependsOn` | Yes | `[]` if none |
| `dependencyPolicy` | Yes | Always `"block"` |

---

## Story States

| Status | Meaning |
|--------|---------|
| `pending` | Not started yet |
| `in_progress` | Currently running |
| `passes: true` | Passed — skipped on re-run |
| `failed` | Failed last attempt — will retry up to max_attempts |
| `blocked` | Exhausted max attempts — manual reset required |

---

## Pre-Run Checklist

Before launching Ralph:

- [ ] Every story that modifies an existing file has that file in `contextFiles`
- [ ] Every AC is a literal shell command
- [ ] No em-dashes or Unicode in any text field
- [ ] Every story has `qualityChecks`
- [ ] **Run every AC manually** — if it fails in your terminal, it fails for Ralph
- [ ] Risky/integration stories come before polish in priority order

---

## Common Failures and Fixes

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| Ralph loops reading same file 3x | File not in `contextFiles` | Put file in `contextFiles` with structural spec |
| Ralph rewrites a file already correct | No "current state" in description | Add "File already exists — review before modifying" |
| Story passes but logic wrong | AC was prose, not executable | Replace with `python3 tests/test_X.py` |
| Story fails every attempt | Scope too large | Split into 2–3 smaller stories |
| AC fails with `command not found: p` | `qualityChecks` was a string, iterated char-by-char | Must be a list `["command"]` |
| write_file rejected: truncation | Model internally truncated, wrote `... [truncated]` | tools.py catches this; Ralph re-reads and rewrites |
| Story hits max_attempts, goes silent | Silent failure | BLOCKED status + notification. Reset `passes: false, attempts: 0` |
| 500 OOM from llama-server | Context too large | Keep files < 200 lines; increase `--ctx-size` |
| CRITIQUE stage times out | File too large for single-stage read | Split into smaller stories; keep files < 400 lines |
| configparser AC always fails | configparser lowercases dict keys | Compare lowercase: `{k.lower(): v for k, v in dict(...).items()}` |

---

## Intervening When Something Goes Wrong

### Story BLOCKED (max attempts reached)

```bash
docker compose run --rm ralph python3 -c "
import json
prd = json.load(open('/app/projects/{slug}/prd.json'))
story = next(s for s in prd['userStories'] if s['id'] == 'US-001')
story['passes'] = False
story['attempts'] = 0
story.pop('status', None)
story.pop('error', None)
json.dump(prd, open('/app/projects/{slug}/prd.json', 'w'), indent=2)
"
```

Then re-run:
```bash
docker compose run --rm ralph {slug} --story US-001
```

### Lockfile collision

```bash
rm ~/ralph/projects/{slug}/.pipeline.lock
```

### Container won't start

```bash
docker compose ps
docker compose logs ralph
docker compose run --rm ralph python3 -c "import sys; print(sys.version)"
```

---

## Ralph's Policy on Manual Fixes

> When Ralph hits a wall, fix Ralph's architecture — do not manually patch code. Manual code fixes are a last resort after architecture fixes are exhausted.

---

## Re-Running a Completed Project

Stories with `passes: true` are skipped. Reset all to re-run:

```bash
docker compose run --rm ralph python3 -c "
import json
with open('/app/projects/{slug}/prd.json') as f:
    prd = json.load(f)
for s in prd['userStories']:
    s['passes'] = False
    s['attempts'] = 0
    s.pop('status', None)
    s.pop('error', None)
    s.pop('lastAttempt', None)
with open('/app/projects/{slug}/prd.json', 'w') as f:
    json.dump(prd, f, indent=2)
"
```

Then:
```bash
docker compose run --rm ralph {slug}
```

---

## Configuration

`config.yaml` controls model URL, limits, and timeouts:

| Setting | Default | Notes |
|---------|---------|-------|
| `model_url` | `http://localhost:8090/v1` | Container reaches host via host networking |
| `max_tokens` | `16384` | Per-model-call limit |
| `max_context_tokens` | `120000` | ~11K headroom under 131072 ctx |
| `request_timeout` | `14400` | 4-hour timeout per request |
| `max_attempts_per_story` | `5` | Stories go to BLOCKED after 5 failures |
| `max_tool_calls_per_story` | `160` | Safety cap per story attempt |
