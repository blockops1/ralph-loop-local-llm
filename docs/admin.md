# Ralph Loop — Admin Guide (Docker)

**Version:** 0.6.0 (2026-04-16)
**Location:** `/Users/jill/ralph/`
**Container image:** `ralph-local:latest`

---

## What It Is

Ralph is an autonomous coding loop. It reads a `prd.json`, executes user stories one at a time using a local LLM, verifies each story with acceptance criteria, and notifies via Discord/Telegram.

**Ralph runs in Docker.** The containerized Ralph has a minimal blast radius — it can only write to mounted volumes and shares the host network stack for LLM access.

---

## Architecture

```
Host Mac Mini                        Docker container (ralph)
─────────────────────                ─────────────────────────────────
~/ralph/projects/  ───── volume ──►  /app/projects/
~/ralph/logs/     ───── volume ──►  /app/logs/
llama-server :8090 ◄── host net ──►  localhost:8090
config.yaml         ─────────────►  /app/config.yaml (read at runtime)
PROMPT*.md          ─────────────►  /app/PROMPT*.md

docker compose run ralph {slug}  ──►  ENTRYPOINT python3 pipeline_runner.py
pipeline_runner.py               ──►  3-stage pipeline orchestrator
```

**LLM backend:** `llama-server` running on Mac Mini host at `http://localhost:8090/v1` (container uses host networking)
**Model:** `Qwen3.5-27B-Q6_K.gguf` via launchd plist
**Ralph source:** `~/ralph/` (bind-mounted at build time via `COPY . .` in Dockerfile)

---

## The 3-Stage Pipeline

Each story runs through three completely isolated stages. The creator never reviews its own work — every stage is a fresh model call with no memory of the previous one.

```
Story picked from prd.json
        │
        ├─► STAGE 1 — CREATE
        │   ralph.py (run_story_loop type=create)
        │   + PROMPT.md + story description + contextFiles
        │   Writes output to /app/projects/{slug}/code/
        │   Git commit
        │
        ├─► STAGE 2 — CRITIQUE
        │   Single raw model call (no tools)
        │   + PROMPT-critique.md + story JSON + output file
        │   Writes critique.md next to the output file
        │   (fresh model context — sees only what was built, not how)
        │
        └─► STAGE 3 — FIX
            ralph.py (run_story_loop type=rework)
            + PROMPT-rework.md + critique.md + output file
            Overwrites output file with corrections
            Git commit

prd.json updated → next story
```

**Stage isolation principle:** CREATE builds, CRITIQUE reviews without having written the code, FIX reworks without having created it. Each stage is blind to the others' process.

**FIX always runs** — even if CRITIQUE passes. It validates and can catch issues the critique missed.

---

## Running Ralph

### Prerequisites

**llama-server must be running on the host first:**

```bash
llama-server \
  --model ~/.cache/llama.cpp/Qwen3.5-27B-Q6_K.gguf \
  --port 8090 \
  --host 127.0.0.1 \
  --ctx-size 131072 \
  -ngl 99 \
  --chat-template chatml \
  --flash-attn on \
  --parallel 1
```

```bash
# Verify it's up
curl -s http://127.0.0.1:8090/v1/models
```

To manage the launchd service:

```bash
# Status
launchctl list | grep llama

# Restart
launchctl unload ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
launchctl load ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
```

### Docker Commands

```bash
cd ~/ralph

# Build the image (first time or after Dockerfile changes)
docker compose build

# Run the 3-stage pipeline for a project
docker compose run ralph {project-slug}

# Run for a single story only
docker compose run ralph {project-slug} --story US-001

# List all projects
docker compose run --rm ralph python3 pipeline_runner.py --list-projects

# Watch live log output inside the container
docker compose run --rm ralph tail -f /app/logs/$(ls -t /app/logs/ | head -1)
```

On the host, logs are also accessible at:

```bash
ls ~/ralph/logs/
tail -f ~/ralph/logs/pipeline-{slug}-{timestamp}.log
```

---

## PRD Path Convention (Critical)

Ralph runs inside the container where `RALPH_DIR=/app`. All file paths in `prd.json` must use **container absolute paths**, never host paths.

| What | Wrong (host path) | Correct (container path) |
|------|-------------------|---------------------------|
| `target_file` | `ralph/projects/slug/code/foo.py` | `/app/projects/slug/code/foo.py` |
| `output_file` | `ralph/projects/slug/code/foo.py` | `/app/projects/slug/code/foo.py` |
| `contextFiles` | `ralph/projects/slug/code/foo.py` | `/app/projects/slug/code/foo.py` |
| `qualityChecks` commands | `python3 -m py_compile ralph/projects/...` | `python3 -m py_compile /app/projects/...` |
| `acceptanceCriteria` commands | same | same |

**Every path reference in the PRD must be prefixed with `/app/projects/` not `ralph/projects/`.**

The actual volume mount is `~/ralph/projects:/app/projects` — so `ralph/projects/foo` inside the container resolves to `/app/ralph/projects/foo` which does not exist. The correct prefix is always `/app/projects/`.

`tools.py` enforces this: `write_file` and `copy_file` reject bare filenames (no slashes) and any path that doesn't start with `/app/projects/`.

---

## Ralph Should Never Touch Production Code Directly

**Rule:** Ralph works on copies, not originals. Production code lives at `~/production_apps/{project}/`. Ralph's workspace is `~/ralph/projects/{slug}/code/`.

**Workflow:**
1. Copy production files into `~/ralph/projects/{slug}/code/` before running Ralph
2. Ralph edits files in its container workspace at `/app/projects/{slug}/code/`
3. After all stories pass, Jill reviews and copies modified files back to `~/production_apps/{project}/scripts/`
4. Ralph never reads from or writes to `~/production_apps/` directly

This protects live trading systems from experimental Ralph output that hasn't been reviewed.

---

## Project Directory Structure

```
~/ralph/                          # Host source directory
├── Dockerfile
├── docker-compose.yml
├── pipeline_runner.py            # 3-stage pipeline orchestrator (ENTRYPOINT)
├── ralph.py                      # Single-stage orchestrator
├── ralph.sh                      # Shell wrapper (single-stage mode)
├── prd_manager.py                 # PRD read/write, story state machine
├── tools.py                      # Tool registry + execution
├── config.yaml                   # Model URL, limits, timeouts
├── PROMPT.md                     # CREATE stage system prompt
├── PROMPT-critique.md            # CRITIQUE stage system prompt
├── PROMPT-rework.md              # FIX stage system prompt
├── projects/                     # Host path → container mount: ~/ralph/projects:/app/projects
│   └── {slug}/
│       ├── prd.json              # The PRD (source of truth)
│       ├── progress.txt          # Append-only run log
│       ├── AGENTS.md             # Project conventions (optional)
│       └── code/                 # Working copy of source files
└── logs/                        # Host path → container mount: ~/ralph/logs:/app/logs
```

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

## Resetting a Blocked Story

```bash
docker compose run --rm ralph python3 - <<'PYEOF'
import json, sys
slug = "YOUR_SLUG"
story_id = "US-XXX"
d = json.load(open(f"/app/projects/{slug}/prd.json"))
s = next((x for x in d["userStories"] if x["id"] == story_id), None)
if s:
    s["passes"] = False
    s["attempts"] = 0
    s.pop("status", None)
    s.pop("error", None)
    json.dump(d, open(f"/app/projects/{slug}/prd.json", "w"), indent=2)
    print(f"Reset: {story_id}")
else:
    print(f"Story not found: {story_id}")
    sys.exit(1)
PYEOF
```

Then optionally add a hint to `AGENTS.md` before re-running.

---

## Key Config Settings (config.yaml)

| Setting | Value | Notes |
|---------|-------|-------|
| `model_url` | `http://localhost:8090/v1` | Container networking — llama-server on host |
| `model_id` | `Qwen3.5-27B-Q6_K.gguf` | Must match the loaded model |
| `max_tokens` | `16384` | Per-model-call token limit |
| `max_context_tokens` | `60000` | Aligned with llama-server `--ctx-size` |
| `request_timeout` | `14400` | 4-hour timeout per request |
| `max_attempts_per_story` | `5` | Stories go to BLOCKED after 5 failures |
| `max_tool_calls_per_story` | `160` | Safety cap per story attempt |

---

## llama-server Troubleshooting

**Restart if:** model stops responding, generates gibberish, or OOMs.

```bash
# Check it's listening
curl -s http://127.0.0.1:8090/v1/models | jq

# Check running processes
ps aux | grep llama-server

# Full restart via launchd
launchctl unload ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
launchctl load ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
```

---

## Post-Completion: Copying Ralph's Output to Production

Ralph never writes to production. After a project completes:

```bash
# Copy all output files back
cp ~/ralph/projects/{slug}/code/*.py ~/production_apps/{project}/scripts/

# Or for a single file
cp ~/ralph/projects/{slug}/code/{file}.py ~/production_apps/{project}/scripts/{file}.py
```

**Three documents to cross-reference when reviewing a completed Ralph project:**

| Document | Location | Purpose |
|----------|----------|---------|
| **PRD** | `~/ralph/projects/{slug}/prd.json` | What Ralph was asked to do — stories, scope, acceptance criteria |
| **Phase briefing** | `~/production_apps/{project}/docs/Phase-*-briefing.md` | Detailed implementation guidance for that phase |
| **Project plan** | `~/production_apps/{project}/PROJ-*.md` | Roadmap context — which phase this is, what comes next |

After copying to production:
1. Mark the phase complete in the project plan: `Phase N — COMPLETE` + dev commit hash
2. Open Phase N+1's briefing doc to define what comes next
3. Write the next PRD for Ralph

---

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| 0.6.0 | 2026-04-16 | Full 3-stage pipeline documentation, PRD path convention, production workflow, stage isolation principle |
| 0.5.0 | 2026-04-05 | Docker containerization — minimal blast radius, volume-persisted projects/logs |
| 0.4.0 | 2026-03-19 | Subprocess story runner, prd_linter, contextFiles pre-loading |
| 0.3.0 | 2026-03-17 | llama.cpp backend, write-guard, sequential ralph.sh, BLOCKED status |
| 0.2.0 | 2026-03-15 | Switched to Ollama |
| 0.1.0 | 2026-03-10 | Initial release |

---

## Publishing to GitHub

Public repo: `git@github.com:blockops1/ralph-loop-local-llm.git`

Key sanitizations before publishing:
- `/Users/jill/` → `/Users/yourname/`
- `374999219` → `YOUR_CHAT_ID`
- Internal IPs: `10.120.60.x` → `192.168.1.x`
