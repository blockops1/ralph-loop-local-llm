---
name: ralph-loop
description: "Manage the Ralph autonomous coding loop. Ralph runs local instruct models (Qwen 3.5 27B) through structured coding tasks defined in prd.json. Ralph is driven by pipeline_runner.py — a 3-stage pipeline (CREATE → CRITIQUE → FIX). Use when: checking Ralph status, starting a new Ralph project, reviewing Ralph's completed work, intervening when Ralph is stuck, understanding what Ralph did and why. Triggers: 'check ralph status', 'what is ralph working on', 'start ralph', 'ralph done yet', 'ralph is stuck', 'how does ralph work', 'ralph pipeline'."
tags: ["ralph", "autonomous", "coding", "llm", "qwen", "llama.cpp"]
related_skills: ["ralph-prd", "local-llm-manager"]
---

# Ralph Loop Skill

Ralph is the autonomous coding loop that runs a local instruct model (Qwen 3.5 27B via llama.cpp) through structured coding tasks defined in `prd.json`.

## How Ralph Runs

**Primary entry point:** `pipeline_runner.py` (3-stage pipeline)

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
ralph/
├── ralph.py              # Single-stage orchestrator (PROMPT.md only) — rarely used
├── ralph.sh              # Shell wrapper — calls pipeline_runner.py, NOT single-stage
├── pipeline_runner.py    # 3-stage pipeline orchestrator — PRIMARY entry point
├── prd_manager.py        # PRD read/write, story state management
├── prd_linter.py         # Validates prd.json before every run — aborts if malformed
├── tools.py              # Tool registry + execution (git_commit, write_file, etc.)
├── config.yaml           # Model URL, limits, timeouts
├── PROMPT.md             # System prompt for CREATE stage (has Tool Usage Notes)
├── PROMPT-critique.md    # System prompt for CRITIQUE stage
├── PROMPT-rework.md      # System prompt for FIX stage
├── logs/                 # Timestamped run logs
├── projects/             # One subdir per project
│   └── {slug}/
│       ├── prd.json      # The PRD (source of truth)
│       ├── progress.txt  # Append-only run log
│       ├── AGENTS.md     # Project conventions (optional)
│       ├── critique.md   # Stage 2 output (per project)
│       ├── .pipeline.lock  # Concurrency lock — remove if stale
│       └── archive/      # Archived runs (reset before fresh start)
└── projects/example/    # Minimal working example
```

## Launchd Management

Ralph is managed by `com.user.ralph-runner.plist` (launchd, ~/Library/LaunchAgents/):

```xml
<key>RunAtLoad</key><false/>   <!-- Disabled — enable when project is active -->
<key>StartInterval</key><integer>600</integer>  <!-- Fires every 10 minutes -->
```

- **Status check:** `launchctl list | grep ralph` — exit 1 means not running
- **PID check:** `ps aux | grep pipeline_runner | grep -v grep`
- **Enabling:** `launchctl load ~/Library/LaunchAgents/com.user.ralph-runner.plist`
- **Disabling:** `launchctl unload ~/Library/LaunchAgents/com.user.ralph-runner.plist`
- **NOT auto-restarting:** pkill stops it — launchd will fire again at next 10min interval

## Quick Status Check

```bash
cd ~/ralph
python3 pipeline_runner.py --list-projects
```

Or check a specific project:

```bash
# Stories done / total
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
tail -30 logs/pipeline-{slug}-*.log | tail -30
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
nohup python3 pipeline_runner.py {slug} > logs/pipeline-{slug}-$(date +%Y%m%d_%H%M%S).log 2>&1 &
echo "PID: $!"
```

Or let launchd handle it (fires every 10min if enabled):

```bash
launchctl load ~/Library/LaunchAgents/com.user.ralph-runner.plist
```

Ralph sends a Discord/Telegram notification when each story completes.

## ralph.sh Is Not Single-Stage

**Important correction:** `ralph.sh` does NOT run single-stage Ralph. It calls `pipeline_runner.py` (the 3-stage pipeline). The skill's earlier description of "ralph.sh single-stage for quick iterations" is deprecated.

**What ralph.sh actually does:**
- Validates PRD via `prd_linter.py` (aborts if malformed)
- Flips CPU governor to performance mode during run, restores on exit
- Runs `pipeline_runner.py` with nohup + wait (sequential, not parallel)
- Log rotates — keeps last 20 logs per project

**When to use ralph.sh:** Only when launching via launchd/cron. For manual runs, use `pipeline_runner.py` directly.

## Model Configuration

Ralph uses Qwen 3.5 27B Q6_K via launchd-managed llama-server on port 8090.

**llama-server is managed by:** `ai.hermes.llama-server-opus.plist` (~/Library/LaunchAgents/)
- **Restart server:** `launchctl unload ... && launchctl load ...`
- **Verify:** `curl -s http://127.0.0.1:8090/v1/models`

**config.yaml (ralph/config.yaml):**
```yaml
model_url: "http://localhost:8090/v1"
model_id: "Qwen3.5-27B-Q6_K.gguf"
max_tokens: 16384
max_context_tokens: 60000
request_timeout: 14400
max_tool_calls_per_story: 160
max_attempts_per_story: 5
```

**No thinking:** `chat_template_kwargs: {"enable_thinking": false}` in the API call. Thinking mode causes malformed tool calls.

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

Then optionally add a hint to `AGENTS.md` and re-run.

### Lockfile collision

```bash
rm ~/ralph/projects/{slug}/.pipeline.lock
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
python3 pipeline_runner.py {slug} --story US-001
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

## Context Pre-Loading

Ralph pre-loads all files listed in `contextFiles` directly into the system prompt. This eliminates re-read loops.

- contextFiles count against `max_context_tokens`
- Always put files Ralph will modify in contextFiles
- If contextFiles are large, increase llama-server `--ctx-size` before increasing `max_context_tokens`

## write_file Truncation Guard

`tools.py` rejects write_file calls containing truncation artifacts (`"... [truncated]"`, `"# ... truncated"`). If this fires, Ralph re-reads and writes the complete file.

## Reviewing Completed Work

```bash
cd ~/.hermes/workspace
git log --oneline -10 -- ralph/
git log --oneline -5 -- {target-repo}/   # Where Ralph committed
```

Each passed story = one git commit. After full PRD complete → merge dev to main.

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
    │  - python3 pipeline_runner.py {slug}
    │  - Wait for notifications
    │  - Intervene if blocked
    │
    ▼
Review git log + commits
```

**Never skip ralf-prd.** A bad PRD wastes hours of model time.

## Files to Know

| File | Purpose |
|------|---------|
| `ralph.py` | Single-stage orchestrator (legacy, rarely used) |
| `ralph.sh` | Shell wrapper — calls pipeline_runner.py, manages CPU governor, log rotation |
| `pipeline_runner.py` | 3-stage pipeline orchestrator — PRIMARY |
| `prd_linter.py` | Validates prd.json before every run |
| `prd_manager.py` | PRD CRUD, story state machine |
| `tools.py` | Tool registry + execution (git_commit uses temp file, no dd) |
| `config.yaml` | Model URL, limits, timeouts |
| `PROMPT.md` | CREATE stage system prompt + Tool Usage Notes |
| `PROMPT-critique.md` | CRITIQUE stage system prompt |
| `PROMPT-rework.md` | FIX stage system prompt |
| `logs/*.log` | Timestamped run logs |
| `projects/{slug}/prd.json` | Project source of truth |
| `projects/{slug}/progress.txt` | Append-only story log |
| `projects/{slug}/critique.md` | Stage 2 output (per project) |
| `projects/{slug}/.pipeline.lock` | Concurrency lock |
| `projects/{slug}/AGENTS.md` | Project conventions |