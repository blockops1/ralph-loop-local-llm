# Ralph Loop — Admin Guide (Docker)

**Version:** 0.5.0 (2026-04-05)
**Location:** `/Users/jill/ralph/`
**Container image:** `ralph-local:latest`

---

## What It Is

Ralph is an autonomous coding loop. It reads a `prd.json`, executes user stories one at a time using a local LLM, verifies each story with acceptance criteria, and notifies via Discord/Telegram.

**This version runs in Docker.** The containerized Ralph has a minimal blast radius — it can only write to mounted volumes.

---

## Architecture

```
ralph.sh (host wrapper)
  └─→ docker compose run --rm ralph
        └─→ pipeline_runner.py (3-stage pipeline)
              ├─→ ralph.py / loop_runner.py
              ├─→ tools.py
              ├─→ prd_manager.py
              └─→ prd_linter.py
```

**LLM backend:** `llama-server` running on Mac Mini host at `http://host.docker.internal:8090` (or `http://localhost:8090` if running outside Docker)
**Model:** `Qwen3.5-27B-Q6_K.gguf` via host launchd plist
**Config:** `config.yaml` in the repo root

---

## Quick Start

```bash
cd ~/ralph

# Build the image (first time only)
docker compose build

# Run the pipeline — auto-selects first active project
./ralph.sh

# Run a specific project
./ralph.sh my-project-slug

# Run a specific story
./ralph.sh my-project-slug --story US-001

# List all projects
docker compose run --rm ralph python3 pipeline_runner.py --list-projects
```

---

## Project Data Locations

| What | Where |
|------|-------|
| Project PRDs + code | Docker named volume `ralph-projects` → `~/ralph-data/projects/` |
| Pipeline logs | Docker named volume `ralph-logs` → `~/ralph-data/logs/` |
| Ralph source | This repo (`~/ralph/`) |

To inspect project files on the host:

```bash
ls ~/ralph-data/projects/
docker compose run --rm ralph ls /app/projects/
```

---

## llama-server Management

llama-server runs on the Mac Mini host via launchd, NOT inside the container.

```bash
# Status
launchctl list | grep llama

# Restart
launchctl unload ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist
launchctl load ~/Library/LaunchAgents/ai.hermes.llama-server-opus.plist

# Verify it's up
curl -s http://127.0.0.1:8090/v1/models
```

---

## Story States

| Status | Meaning |
|--------|---------|
| `pending` | Not started |
| `in_progress` | Running |
| `passes: true` | Passed |
| `failed` | Failed last attempt — will retry |
| `blocked` | Exhausted max attempts — manual reset needed |

---

## Resetting a Blocked Story

```bash
docker compose run --rm ralph python3 - <<'PYEOF'
import json
d = json.load(open('/app/projects/YOUR_SLUG/prd.json'))
for s in d['userStories']:
    if s['id'] == 'US-XXX':
        s['passes'] = False
        s['attempts'] = 0
        s.pop('status', None)
        s.pop('error', None)
json.dump(d, open('/app/projects/YOUR_SLUG/prd.json', 'w'), indent=2)
print('Reset:', 'US-XXX')
PYEOF
```

---

## Key Config Settings

| Setting | Value | Notes |
|---------|-------|-------|
| `model_url` | `http://host.docker.internal:8090/v1` | Container networking — llama-server on host |
| `model_id` | `Qwen3.5-27B-Q6_K.gguf` | Must match the loaded model |
| `max_attempts_per_story` | 5 | Stories blocked after 5 failures |
| `max_context_tokens` | 60000 | Aligned with llama-server `--ctx-size` |

---

## Logs

```bash
# Tail the latest pipeline log inside the container
docker compose run --rm ralph tail -f /app/logs/$(ls -t /app/logs/ | head -1)

# Or on the host
ls ~/ralph-data/logs/
tail -f ~/ralph-data/logs/*.log
```

---

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
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
