---
name: ralph-loop
description: "Manage the Ralph autonomous coding loop. Ralph runs local instruct models (Qwen 3.5 27B via llama.cpp) through structured coding tasks defined in prd.json. Ralph runs inside a Docker container (via docker compose). Execution is a 3-stage pipeline: CREATE → CRITIQUE → FIX. Use when: checking Ralph status, starting a new Ralph project, reviewing Ralph's completed work, intervening when Ralph is stuck, understanding what Ralph did and why. Triggers: 'check ralph status', 'what is ralph working on', 'start ralph', 'ralph done yet', 'ralph is stuck', 'how does ralph work', 'ralph pipeline'."
tags: ["ralph", "autonomous", "coding", "llm", "qwen", "llama.cpp", "docker"]
related_skills: ["ralph-prd", "local-llm-manager"]
---

# Ralph Loop Skill

Ralph is the autonomous coding loop that runs a local instruct model (Qwen 3.5 27B via llama.cpp) through structured coding tasks defined in `prd.json`.

## How Ralph Runs

**Ralph runs inside Docker.** The Mac Mini host has no Python dependencies — everything runs in the `ralph-local` container.

**Entry point:** `ralph.sh` (Docker wrapper) → `docker compose run --rm ralph` → `python3 ralph.py` (single-stage) OR `python3 pipeline_runner.py` (3-stage)

```
Story picked from prd.json (get_next_story)
    │
    ├─→ stage_create():   ralph.run_story_loop(type=create) + PROMPT.md
    │   └─→ write_file() → output file
    │   └─→ git commit (via temp file — no shell dd)
    │
    ├─→ stage_critique():  raw model call + PROMPT-critique.md + PRD + output file
    │   └─→ writes critique.md next to output file
    │
    └─→ stage_fix():       ralph.run_story_loop(type=rework) + PROMPT-rework.md + critique + output
        └─→ write_file() → overwrites output file
        └─→ git commit

prd.json updated → next story
```

**Key design principle:** The creator never reviews its own work. Each stage is a completely fresh model context — no memory, no bias.

**FIX always runs** — even if CRITIQUE passes. It validates and catches issues the critique missed.

## Ralph Directory Structure

```
~/ralph/                          # Ralph root (on Mac Mini host)
├── Dockerfile                     # Builds ralph-local image
├── docker-compose.yml             # Container config with host networking
├── ralph.sh                      # Docker wrapper script — PRIMARY LAUNCHER
├── ralph.py                      # Single-stage orchestrator (called inside container)
├── pipeline_runner.py            # 3-stage pipeline (called inside container)
├── prd_manager.py                # PRD read/write, story state management
├── prd_linter.py                 # Validates prd.json before every run
├── tools.py                      # Tool registry + execution
├── config.yaml                   # Model URL, limits, timeouts
│                                  #   model_url: http://host.docker.internal:8090/v1 (Docker)
│                                  #   For native: http://localhost:8090/v1
├── PROMPT.md                     # CREATE stage system prompt
├── PROMPT-critique.md            # CRITIQUE stage system prompt
├── PROMPT-rework.md              # FIX stage system prompt
├── logs/                         # Timestamped run logs (host volume)
├── projects/                     # One subdir per project (host volume)
│   └── {slug}/
│       ├── prd.json             # The PRD (source of truth)
│       ├── progress.txt         # Append-only run log
│       ├── AGENTS.md            # Project conventions (optional)
│       ├── critique.md          # Stage 2 output (per project)
│       ├── .pipeline.lock       # Concurrency lock — remove if stale
│       └── archive/             # Archived runs
└── skills/
    ├── ralph-loop/              # THIS skill
    └── ralph-prd/               # PRD writing skill
```

**Volumes:** `projects/` and `logs/` are mounted from host → container. Code files are in the image.

## Prerequisites

1. **llama-server running on host** (port 8090, managed by launchd):
   ```bash
   launchctl list | grep llama-server-opus
   curl -s http://127.0.0.1:8090/health  # should return {"status":"ok"}
   ```
   Restart if needed: `launchctl unload ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist && launchctl load ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist`

2. **Docker Desktop running** on the Mac Mini

3. **Ralph image built** (one-time):
   ```bash
   cd ~/ralph && docker compose build
   ```

## Quick Status Check

```bash
cd ~/ralph
docker compose ps                    # Is Ralph container running?
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

## Starting a Project

### 1. Write the PRD

Use the **ralph-prd skill** first. A good PRD is the #1 determinant of success.

### 2. Reset if re-running

```bash
cd ~/ralph/projects/{slug}
mkdir -p archive && mv .pipeline.lock archive/ 2>/dev/null || true

# Reset all stories
python3 -c "
import json
prd = json.load(open('prd.json'))
for s in prd['userStories']:
    s['passes'] = False
    s['attempts'] = 0
    s.pop('status', None)
    s.pop('error', None)
json.dump(prd, open('prd.json','w'), indent=2)
print('Reset', len(prd['userStories']), 'stories')
"
```

### 3. Launch

```bash
cd ~/ralph

# Standard: 3-stage pipeline
./ralph.sh {slug}

# Single story only
./ralph.sh {slug} --story US-001

# List projects
./ralph.sh --list-projects
```

**Ralph sends a Discord/Telegram notification** when each story completes and when the pipeline finishes.

## How ralph.sh Works

`ralph.sh` is the Docker wrapper:

```bash
docker compose run --rm \
  --entrypoint "python3 ralph.py" \
  ralph {slug}
```

For 3-stage pipeline (standard):
```bash
docker compose run --rm \
  --entrypoint "python3 pipeline_runner.py {slug}" \
  ralph
```

Key behavior:
- Builds image if needed (`docker compose build --quiet`)
- Mounts `projects/` and `logs/` from host
- Uses `host.docker.internal:8090` to reach llama-server on host
- `--rm` cleans up container after each run

## Model Configuration

**llama-server** runs on the Mac Mini host (not in container) managed by launchd:
- Plist: `~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist`
- Model: `Qwen3.5-27B-Q6_K.gguf` on port 8090
- Thinking disabled: `chat_template_kwargs: {"enable_thinking": false}`

**Ralph config.yaml** (inside container):
```yaml
# Ralph runs IN Docker — this URL reaches the host's llama-server
model_url: "http://host.docker.internal:8090/v1"
model_id: "Qwen3.5-27B-Q6_K.gguf"
max_tokens: 8192
max_context_tokens: 120000   # ~11K headroom under 131072 ctx
request_timeout: 14400        # 2 hours — cold prefill can be slow
max_tool_calls_per_story: 160
max_attempts_per_story: 5
```

**For native testing (no Docker):** change to `http://localhost:8090/v1` in `config.yaml`.

## Tool Usage Rules (from PROMPT.md)

Ralph's system prompt explicitly directs:
- **File existence:** Use `test -f path` via `run_command` — `python3 -c` inline is blocked for security
- **Directory listing:** Use `list_dir`, not `ls` through run_command
- **File reading:** Use `read_file`, not `cat` through run_command
- **Creating files:** If a file doesn't exist, create with `write_file` — do NOT call `run_command touch`

## Intervening When Something Goes Wrong

### Story BLOCKED (max attempts reached)

Ralph sends a notification with story ID and last error. To reset:

```python
import json
prd = json.load(open('~/ralph/projects/{slug}/prd.json'))
story = next(s for s in prd['userStories'] if s['id'] == 'US-001')
story['passes'] = False
story['attempts'] = 0
story.pop('status', None)
story.pop('error', None)
json.dump(prd, open('prd.json', 'w'), indent=2)
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
docker compose build        # rebuild image
docker compose run --rm ralph python3 pipeline_runner.py --list-projects  # test
```

### Fix a broken story description

Never manually edit Ralph's output. Fix the PRD:

```bash
code ~/ralph/projects/{slug}/prd.json
python3 -c "
import json
prd = json.load(open('ralph/projects/{slug}/prd.json'))
s = next(x for x in prd['userStories'] if x['id'] == 'US-001')
s['attempts'] = 0
s.pop('error', None)
json.dump(prd, open('prd.json','w'), indent=2)
"
cd ~/ralph && ./ralph.sh {slug} --story US-001
```

### Ralph's policy on manual fixes

> When Ralph hits a wall, fix Ralph's architecture — do not manually patch code. Manual code fixes are last resort after architecture fixes are exhausted.

---

## Known Issues / Fixes Already Applied

| Issue | Fix | File |
|-------|-----|------|
| `dd` blocked in autonomous mode — git_commit loops | Write commit message to temp file via Python, use `git commit -F` | tools.py |
| `python3 -c` blocked — Ralph loops checking file existence | Added Tool Usage Notes to PROMPT.md — use `test -f` | PROMPT.md |
| Loop detection on `list_dir` with same args | Ralph re-reads from context instead of re-listing | — |
| Docker registry unreachable — pre-built image | `ralph-local:latest` image exists, `docker compose build` updates it | — |

---

## Re-Running a Completed Project

Stories with `passes: true` are skipped. Reset all to re-run:

```python
import json
with open('~/ralph/projects/{slug}/prd.json') as f:
    prd = json.load(f)
for s in prd['userStories']:
    s['passes'] = False
    s['attempts'] = 0
    s.pop('status', None)
    s.pop('error', None)
    s.pop('lastAttempt', None)
with open('prd.json', 'w') as f:
    json.dump(prd, f, indent=2)
```

Then:
```bash
cd ~/ralph && ./ralph.sh {slug}
```

## Context Pre-Loading

Ralph pre-loads all files listed in `contextFiles` directly into the system prompt. This eliminates re-read loops.

- contextFiles count against `max_context_tokens`
- Always put files Ralph will modify in contextFiles
- If contextFiles are large, increase llama-server `--ctx-size` before increasing `max_context_tokens`

## write_file Truncation Guard

`tools.py` rejects write_file calls containing truncation artifacts (`"... [truncated]"`, `"# ... truncated"`). If this fires, Ralph re-reads and writes the complete file.

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

## Files to Know

| File | Purpose |
|------|---------|
| `ralph.sh` | Docker wrapper — PRIMARY entry point |
| `docker-compose.yml` | Container config; `host.docker.internal` for llama-server |
| `ralph.py` | Single-stage orchestrator (legacy; ralph.sh calls pipeline_runner.py by default) |
| `pipeline_runner.py` | 3-stage pipeline orchestrator — called by ralph.sh |
| `prd_linter.py` | Validates prd.json before every run |
| `prd_manager.py` | PRD CRUD, story state machine |
| `tools.py` | Tool registry + execution (git_commit uses temp file, no dd) |
| `config.yaml` | Model URL (`host.docker.internal:8090` for Docker), limits, timeouts |
| `PROMPT.md` | CREATE stage system prompt + Tool Usage Notes |
| `PROMPT-critique.md` | CRITIQUE stage system prompt |
| `PROMPT-rework.md` | FIX stage system prompt |
| `logs/*.log` | Timestamped run logs |
| `projects/{slug}/prd.json` | Project source of truth |
| `projects/{slug}/progress.txt` | Append-only story log |
| `projects/{slug}/critique.md` | Stage 2 output (per project) |
| `projects/{slug}/.pipeline.lock` | Concurrency lock |
