# Ralph Loop — Admin Guide

**Version:** 0.7.0 (2026-04-17)
**Canonical skills:** `~/.hermes/skills/openclaw-imports/ralph-loop/` and `ralph-prd/`
**Docker project:** `~/ralph/` (Ralph's source code + runtime workspace)

---

## What Ralph Is

Ralph is an autonomous coding loop. It reads a `prd.json`, executes user stories one at a time using a local LLM (Qwen 3.5 27B via llama.cpp), verifies each story with acceptance criteria, and notifies via Discord/Telegram when complete or blocked.

**Ralph runs in Docker.** The containerized Ralph has a minimal blast radius — it can only write to mounted volumes and shares the host network stack for LLM access.

---

## Architecture

```
Host Mac Mini                           Docker container (ralph)
─────────────────────                    ─────────────────────────────────
~/ralph/projects/   ──── volume ────►   /app/projects/
~/ralph/logs/      ──── volume ────►   /app/logs/
llama-server :8090 ◄─── host net ────►  localhost:8090
config.yaml         ───────────────►   /app/config.yaml (read at runtime)
PROMPT*.md          ───────────────►   /app/PROMPT*.md
ralph.sh            ──── docker ────►  python3 ralph.py
ralph.py            ──── calls ────►   loop_runner.py → run_all_through_pipeline()
```

**LLM backend:** `llama-server` on Mac Mini host at `http://localhost:8090/v1` (container uses host networking via `network_mode: host`)
**Model:** `Qwen3.5-27B-Q6_K.gguf` via launchd plist

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
        │   Git commit (via temp file — no shell dd)
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

1. **llama-server running on host** (port 8090, managed by launchd):
   ```bash
   launchctl list | grep llama-server-opus
   curl -s http://127.0.0.1:8090/health  # should return {"status":"ok"}
   ```
   Restart if needed:
   ```bash
   launchctl unload ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
   launchctl load ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
   ```

2. **Docker Desktop running** on the Mac Mini

3. **Ralph image built** (one-time):
   ```bash
   cd ~/ralph && docker compose build
   ```

### Launch Commands

```bash
cd ~/ralph

# Standard: 3-stage pipeline (CREATE → CRITIQUE → FIX per story)
./ralph.sh {slug}

# Single story only
./ralph.sh {slug} --story US-001

# List all projects
./ralph.sh --list-projects
```

**Ralph sends a Discord/Telegram notification** when each story completes and when the pipeline finishes.

### How ralph.sh Works

`ralph.sh` is the Docker wrapper:

```bash
docker compose run --rm \
  --entrypoint "python3 ralph.py" \
  ralph {slug}
```

`ralph.py` detects `--pipeline` (default) vs `--single-stage` and calls `loop_runner.py` → `run_all_through_pipeline()` for the 3-stage pipeline.

Key behavior:
- Builds image if needed (`docker compose build --quiet`)
- Mounts `projects/` and `logs/` from host
- Uses `host.docker.internal:8090` to reach llama-server on host
- `--rm` cleans up container after each run

### Quick Status Check

```bash
cd ~/ralph
docker compose ps                    # Is Ralph container running?

# Story progress
python3 -c "
import json
d = json.load(open('projects/{slug}/prd.json'))
stories = d.get('userStories', [])
done = [s for s in stories if s.get('passes')]
blocked = [s for s in stories if s.get('status') == 'blocked']
print(f'{len(done)}/{len(stories)} done, {len(blocked)} blocked')
for s in blocked: print(f'  BLOCKED: {s[\"id\"]} — {s.get(\"error\",\"\")[:80]}')
"

# Latest log
ls -lt logs/ | head -5
tail -30 logs/pipeline-{slug}-*.log 2>/dev/null | tail -30
```

---

## PRD Path Convention (Critical)

Ralph runs inside the container where `RALPH_DIR=/app`. All file paths in `prd.json` must use **container absolute paths**, never host paths.

| What | Wrong (host path) | Correct (container path) |
|------|-------------------|--------------------------|
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
3. After all stories pass, review and copy modified files back to `~/production_apps/{project}/scripts/`
4. Ralph never reads from or writes to `~/production_apps/` directly

This protects live trading systems from experimental Ralph output that hasn't been reviewed.

---

## Tool Usage Rules

Ralph's system prompt directs it to use specific tools, not shell equivalents:

- **File existence:** Use `test -f path` via `run_command` — `python3 -c` inline is blocked for security
- **Directory listing:** Use `list_dir`, not `ls` through `run_command`
- **File reading:** Use `read_file`, not `cat` through `run_command`
- **Creating files:** If a file doesn't exist, create with `write_file` — do NOT call `run_command touch`

---

## Known Issues / Fixes Already Applied

| Issue | Fix | File |
|-------|-----|------|
| `dd` blocked in autonomous mode — git_commit loops | Write commit message to temp file via Python, use `git commit -F` | tools.py |
| `python3 -c` blocked — Ralph loops checking file existence | Use `test -f` via run_command instead | PROMPT.md |
| Loop detection on `list_dir` with same args | Ralph re-reads from context instead of re-listing | — |
| Docker registry unreachable — pre-built image | `ralph-local:latest` image exists, `docker compose build` updates it | — |
| `write_file` truncation | `tools.py` rejects truncation artifacts (`"... [truncated]"`) — Ralph re-reads and rewrites | tools.py |

---

## Context Pre-Loading

Ralph pre-loads all files listed in `contextFiles` directly into the system prompt. This eliminates re-read loops.

- contextFiles count against `max_context_tokens`
- Always put files Ralph will modify in contextFiles
- If contextFiles are large, increase llama-server `--ctx-size` before increasing `max_context_tokens`

---

## Intervening When Something Goes Wrong

### Story BLOCKED (max attempts reached)

Ralph sends a notification with story ID and last error. To reset:

```python
import json
prd = json.load(open('/app/projects/{slug}/prd.json'))
story = next(s for s in prd['userStories'] if s['id'] == 'US-001')
story['passes'] = False
story['attempts'] = 0
story.pop('status', None)
story.pop('error', None)
json.dump(prd, open('/app/projects/{slug}/prd.json', 'w'), indent=2)
```

Then optionally add a hint to `AGENTS.md` and re-run:
```bash
cd ~/ralph && ./ralph.sh {slug} --story US-001
```

### Lockfile collision

```bash
rm ~/ralph/projects/{slug}/.pipeline.lock
```

### Container won't start

```bash
cd ~/ralph
docker compose ps                    # check status
docker compose logs ralph            # see what went wrong
docker compose run --rm ralph python3 -c "import sys; print(sys.version)"  # smoke test
```

### Fix a broken story description

Never manually edit Ralph's output. Fix the PRD:

```bash
# Reset the story
python3 -c "
import json
prd = json.load(open('/app/projects/{slug}/prd.json'))
s = next(x for x in prd['userStories'] if x['id'] == 'US-001')
s['attempts'] = 0
s.pop('error', None)
json.dump(prd, open('/app/projects/{slug}/prd.json', 'w'), indent=2)
"
cd ~/ralph && ./ralph.sh {slug} --story US-001
```

### Ralph's policy on manual fixes

> When Ralph hits a wall, fix Ralph's architecture — do not manually patch code. Manual code fixes are last resort after architecture fixes are exhausted.

---

## Common Failures and Fixes

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Ralph loops reading same file 3x | File not in `contextFiles` + intent-only description | Put file in `contextFiles`. Write structural spec: which function, what pattern, what to preserve. |
| Ralph rewrites a file already correct | No "current state" in description | Add "File already exists — review before modifying" |
| Story passes but logic wrong | AC was prose, not executable | Replace with `python3 tests/test_X.py` |
| Story fails every attempt | Scope too large | Split into 2–3 smaller stories |
| AC fails with `command not found: p` | `qualityChecks` was a string and got iterated char-by-char | Must be a list `["command"]` |
| write_file rejected: truncation | Model truncated internally, wrote `... [truncated]` | `tools.py` catches this; Ralph re-reads and rewrites |
| Story hits max_attempts, goes silent | Silent failure | BLOCKED status + notification. Reset `passes: false, attempts: 0` |
| 500 OOM from llama-server | Context too large during generation | Keep one file per story; keep files < 200 lines |
| CRITIQUE stage times out | File too large + large PRD context; exceeds gateway timeout | Split into smaller stories; keep files < 400 lines; use single-stage for large rewrites |
| git_commit fails with 'nc not allowed' | `git status --porcelain` blocked in some environments | Non-fatal — call `task_complete` anyway; work is on disk |
| configparser AC always fails | configparser lowercases dict keys | Compare lowercase keys: `{k.lower(): v for k, v in dict(c['Service']).items()}` |

---

## CRITIQUE Stage — Timeout Prevention

The 3-stage pipeline's CRITIQUE stage is a **single model call** that reads the entire output file and the full PRD JSON. This creates a timeout risk on the Hermes gateway, which has a **10-minute agent command timeout**.

### CRITIQUE Timeout Symptoms

- CRITIQUE never completes: the model takes too long to read + analyze + generate
- File is large (e.g., 990+ lines) with large context (~15,500 tokens)
- Gateway kills the agent command before CRITIQUE finishes

### How to Prevent CRITIQUE Timeouts

**Rule: Keep output files small enough that CRITIQUE can read them in context.**

1. **Split large stories** — if a file exceeds ~400 lines, split into two stories so CRITIQUE only reads the modified portion
2. **Keep contextFiles minimal** — CRITIQUE already loads the full PRD JSON. Do not add unnecessary files to `contextFiles`
3. **Use targeted prompts** — describe only what changed in the story description, not the entire file history
4. **Prefer single-stage** for large file rewrites — the 3-stage CRITIQUE/FIX cycle is designed for modular, incremental work

---

## Sequential Execution Rule

**Never launch multiple `./ralph.sh` or `pipeline_runner.py` calls simultaneously on the same project.** llama-server is single-threaded — parallel runs deadlock.

For chains of PRDs:
```bash
cd ~/ralph && ./ralph.sh slug-1
# wait for completion
cd ~/ralph && ./ralph.sh slug-2
```

For single stories:
```bash
cd ~/ralph && ./ralph.sh {slug} --story US-001
# wait for completion
cd ~/ralph && ./ralph.sh {slug} --story US-002
```

---

## Branch Policy for Production Systems

**Is the target system live?** (cron job runs against real money/APIs without human approval per run)

If YES — Ralph must NOT write to `main`. The PRD must:
1. Start with `git checkout dev` (or `ralph/<slug>` branch)
2. Target all paths on the dev branch
3. Include a final story for test gate and merge procedure

If NO (greenfield/sandbox): target files directly.

**When in doubt, ask Mr. V.**

---

## Project Directory Structure

```
~/ralph/                          # Host source directory
├── Dockerfile                    # Container image definition
├── docker-compose.yml           # Container config; host networking
├── ralph.sh                      # Docker wrapper script — PRIMARY LAUNCHER
├── ralph.py                      # Single-stage orchestrator (called via ralph.sh)
├── pipeline_runner.py            # 3-stage pipeline (standalone; not used by ralph.sh)
├── loop_runner.py                # Pipeline runner logic — called by ralph.py
├── prd_manager.py                # PRD read/write, story state machine
├── prd_linter.py                 # Validates prd.json before every run
├── tools.py                      # Tool registry + execution (git_commit uses temp file, no dd)
├── config.yaml                   # Model URL, limits, timeouts
│                                #   model_url: http://host.docker.internal:8090/v1 (Docker)
│                                #   For native: http://localhost:8090/v1
├── PROMPT.md                     # CREATE stage system prompt
├── PROMPT-critique.md            # CRITIQUE stage system prompt
├── PROMPT-rework.md              # FIX stage system prompt
├── projects/                     # Host path → container mount: ~/ralph/projects:/app/projects
│   └── {slug}/
│       ├── prd.json             # The PRD (source of truth)
│       ├── progress.txt         # Append-only run log
│       ├── AGENTS.md            # Project conventions (optional)
│       ├── critique.md          # Stage 2 output (per project)
│       ├── .pipeline.lock        # Concurrency lock — remove if stale
│       └── archive/             # Archived runs
└── logs/                        # Host path → container mount: ~/ralph/logs:/app/logs
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

## Key Config Settings (config.yaml)

| Setting | Value | Notes |
|---------|-------|-------|
| `model_url` | `http://host.docker.internal:8090/v1` | Container networking — llama-server on host |
| `model_id` | `Qwen3.5-27B-Q6_K.gguf` | Must match the loaded model |
| `max_tokens` | `16384` | Per-model-call token limit |
| `max_context_tokens` | `120000` | ~11K headroom under 131072 ctx |
| `request_timeout` | `14400` | 4-hour timeout per request |
| `max_attempts_per_story` | `5` | Stories go to BLOCKED after 5 failures |
| `max_tool_calls_per_story` | `160` | Safety cap per story attempt |

**For native testing (no Docker):** change to `http://localhost:8090/v1` in `config.yaml`.

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

## Two-Repo Topology — Skills Are Canonical Elsewhere

Ralph's files live in **two separate git repos** with different purposes:

| Repo | Path | Purpose | Skills |
|------|------|---------|--------|
| **Canonical (agent-loaded)** | `~/.hermes/skills/openclaw-imports/` | Skills the agent loads at runtime | YES — canonical |
| **Docker project** | `~/ralph/` | Ralph's source code, Docker setup, project workspaces | NO — skills were archived 2026-04-17 |

**The Docker project repo (`~/ralph/`) previously had a `skills/` subdirectory — that was archived to `~/archive/archive/ralph/skills-20260417/`. Do not recreate it.**

**Rule: All skill edits go to `~/.hermes/skills/openclaw-imports/` only.** That is the canonical location. The Docker project repo is for Ralph's runtime code and project workspaces, not skills.

How to check before editing:
```bash
# Canonical (always edit here):
ls ~/.hermes/skills/openclaw-imports/ralph-loop/SKILL.md

# Docker project (archived — do not edit):
ls ~/ralph/skills/  # should not exist
```

---

## PRD Reference

### PRD Schema

**Root:** `{"userStories": [...]}` — **never `"stories"`** (ralph.py L572 silently skips).

```json
{
  "project": "my-project",
  "slug": "my-project",
  "branchName": "ralph/my-project-feature",
  "description": "What this PRD builds and why.",
  "contextFiles": ["PROJ-my-project.md"],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "userStories": [
    {
      "id": "US-001",
      "title": "One-line title — include filename if creating",
      "type": "create",
      "description": "Current state: ...\n\nWhat to build: ...\n\nConstraints: ...",
      "target_file": "path/to/output.py",
      "output_file": "path/to/output.py",
      "preserve": ["behavior not to change"],
      "acceptanceCriteria": [
        "python3 -m py_compile path/to/output.py",
        "python3 tests/test_output.py"
      ],
      "qualityChecks": ["python3 -m py_compile {file}"],
      "priority": 1,
      "passes": false,
      "attempts": 0,
      "dependsOn": [],
      "dependencyPolicy": "block",
      "contextFiles": ["path/to/existing_file.py"]
    }
  ]
}
```

### Story `type` Field

| type | Meaning |
|------|---------|
| `"create"` | Build a new file or rewrite an existing one from scratch |
| `"review"` | Code review (single-stage only) |
| `"rework"` | Improve/fix an existing file (single-stage only) |

**For 3-stage pipeline:** only `type: "create"` stories are supported. The pipeline's FIX stage handles reworks automatically.

### Pipeline-Specific Fields (3-stage)

When using the 3-stage pipeline:

| Field | Required | Notes |
|-------|----------|-------|
| `type` | Yes | Must be `"create"` for pipeline stories |
| `target_file` | Yes | Path Ralph will write to (usually same as `output_file`) |
| `output_file` | Yes | Same as `target_file` for create stories |
| `preserve` | Recommended | Behaviors to keep unchanged (array of strings) |
| `contextFiles` | Yes | Primary output file should be `contextFiles[0]` |

### Story Fields Reference

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Format: `US-001`, `REV-A`, etc. |
| `title` | Yes | One line with filename |
| `description` | Yes | `Current state:` section mandatory |
| `acceptanceCriteria` | Yes | Shell commands only — exit 0 = pass |
| `qualityChecks` | Yes | At least one command |
| `priority` | Yes | Lower = runs first |
| `passes` | Yes | `false` initially; `true` if already done |
| `attempts` | Yes | Start at `0` |
| `dependsOn` | Yes | `[]` if none |
| `dependencyPolicy` | Yes | Always `"block"` |

---

## Pre-Run Validation Checklist

Before launching Ralph, verify:

- [ ] Every story that modifies an existing file has that file in `contextFiles`
- [ ] Every AC is a literal shell command (grep, `python3 -m py_compile`, etc.)
- [ ] No em-dashes or Unicode in any text field
- [ ] Every story has `qualityChecks`
- [ ] Risky/integration stories come before polish in priority order
- [ ] **TDD stories**: test story (US-00X) has `priority` one lower than its implementation (US-00X+1), and implementation story has `dependsOn: ["US-00X"]`
- [ ] **TDD stories**: test story AC runs the test and passes (even if implementation doesn't exist yet — the test should fail gracefully, not crash)
- [ ] PYTHONPATH: ACs running `python3 tests/test_X.py` — does the test use `from module import`? If yes, set `PYTHONPATH=scripts` in the command
- [ ] **Run every AC manually in your terminal** — if it fails in your terminal, it fails for Ralph
- [ ] Test every acceptance criterion yourself before adding it to the PRD

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

## Re-Running a Completed Project

Stories with `passes: true` are skipped. Reset all to re-run:

```python
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
```

Then:
```bash
cd ~/ralph && ./ralph.sh {slug}
```

---

## Reviewing Completed Work

```bash
# Ralph's git log (commits happen inside container, synced to host via volume)
cd ~/ralph/projects/{slug}
git log --oneline -10

# Target repo (where Ralph committed)
cd ~/production_apps/{target-repo}
git log --oneline -5
```

Each passed story = one git commit inside the container (synced to host volume). After full PRD complete → review and merge.

---

## Ralph PRD Skill → Ralph Loop Skill Handoff

```
Mr. V decides what to build
    │
    ▼
ralph-prd skill: Write prd.json
    │  - Research existing files
    │  - Write stories (one file per story)
    │  - Validate every acceptance criterion
    │  - Verify no Unicode/em-dashes in descriptions
    │
    ▼
ralph-loop skill: Run and monitor
    │  - Reset if re-running
    │  - cd ~/ralph && ./ralph.sh {slug}
    │  - Wait for notifications
    │  - Intervene if blocked
    │
    ▼
Review git log + commits in projects/{slug}/
```

**Never skip ralph-prd.** A bad PRD wastes hours of model time.

---

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| 0.7.0 | 2026-04-17 | Added: tool usage rules, known issues table, context pre-loading, common failures + fixes, CRITIQUE timeout prevention, sequential execution rule, branch policy, pre-run validation checklist, full PRD schema reference. Fixed: ralph.sh entry point clarification. Skills moved to openclaw-imports canonical location. |
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
