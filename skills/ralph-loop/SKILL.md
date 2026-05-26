---
name: ralph-loop
description: "Manage the Ralph autonomous coding loop. Ralph runs local instruct models (Qwen 3.6 35B via llama.cpp) through a 4-stage pipeline defined in prd.json: CREATE -> CRITIQUE -> FIX -> QUALITY. Ralph runs inside a Docker container (via docker compose). QUALITY stage runs Ruff (lint/format) + Pyright (type check) + Desloppify (quality scoring) and injects rework stories back into the PRD. Ralph also supports JS/TS projects via Biome. Use when: checking Ralph status, starting a new Ralph project, reviewing Ralph's completed work, intervening when Ralph is stuck. Triggers: 'check ralph status', 'what is ralph working on', 'start ralph', 'ralph done yet', 'ralph is stuck', 'how does ralph work', 'ralph pipeline'."
tags: ["ralph", "autonomous", "coding", "llm", "qwen", "llama.cpp", "docker"]
related_skills: ["ralph-prd"]
---

# Ralph Loop Skill

Ralph is the autonomous coding loop that runs a local instruct model through structured coding tasks defined in `prd.json`.

## How Ralph Works

**Ralph runs in Docker.** The container has no access to the host filesystem beyond mounted volumes.

**Entry point:** `docker compose run --rm ralph {slug}` runs `pipeline_runner.py` (the Dockerfile ENTRYPOINT).

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
        quality_status.json + rework_stories.json
        Rework stories injected into prd.json for next pass

prd.json updated -> next story
```

**FIX always runs** — even if CRITIQUE passes.

**QUALITY always runs** — it never fails the pipeline (best-effort). It generates rework stories for mechanical issues and injects them into the PRD.

**Key principle:** Each stage is a completely fresh model context — no memory, no bias. The creator never reviews its own work.

---

## Quick Commands

```bash
# Run full pipeline on a project
docker compose run --rm ralph my-project-slug

# Run a single story
docker compose run --rm ralph my-project-slug --story US-001

# List all projects
docker compose run --rm ralph --list-projects
```

---

## Project Directory

Each project lives at `projects/{slug}/`:

```
{slug}/
├── prd.json              ← source of truth (must exist)
├── progress.txt          ← append-only run log
├── critique.md           ← stage 2 output
├── quality_status.json   ← stage 4 output (ruff + pyright + desloppify scores)
├── rework_stories.json   ← stage 4 output (auto-generated fix queue)
└── .pipeline.lock        ← concurrency lock (remove if stale)
```

---

## Prerequisites

1. **llama-server running on host at `http://localhost:8090`** (any OpenAI-compatible server)
2. **Docker Desktop** running
3. **Build image (one time):** `docker compose build`

---

## Status Check

```bash
docker compose ps

python3 -c "
import json
d = json.load(open('projects/{slug}/prd.json'))
stories = d.get('userStories', [])
done = [s for s in stories if s.get('passes')]
blocked = [s for s in stories if s.get('status') == 'blocked']
print(f'{len(done)}/{len(stories)} done, {len(blocked)} blocked')
for s in blocked: print(f'  BLOCKED: {s[\"id\"]} — {s.get(\"error\",\"\")[:80]}')
"
```

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

Then re-run: `docker compose run --rm ralph {slug} --story US-001`

### Lockfile collision

```bash
rm projects/{slug}/.pipeline.lock
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

## Sequential Execution Rule

Never launch multiple `docker compose run --rm ralph` simultaneously on the same project. llama-server is single-threaded — parallel runs deadlock.

---

## Files

| File | Purpose |
|------|---------|
| `pipeline_runner.py` | 4-stage pipeline — PRIMARY ENTRYPOINT |
| `ralph.py` | Legacy orchestrator (for single-stage runs) |
| `prd_manager.py` | PRD CRUD, story state machine |
| `prd_linter.py` | Validates prd.json before every run |
| `tools.py` | Tool registry + execution |
| `config.yaml` | Model URL, limits, timeouts |
| `PROMPT*.md` | Stage system prompts |
| `scripts/quality_pipeline.py` | Stage 4: Ruff + Pyright + Desloppify |

Full details in `README.md`.
