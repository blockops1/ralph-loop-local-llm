# Ralph Loop — Autonomous Local LLM Coding Agent

Ralph is a lightweight autonomous coding loop that reads a Product Requirements Document (PRD), executes user stories one at a time using a local LLM, verifies each story with acceptance tests, and notifies you via Telegram when done.

**No cloud. No API costs. Runs entirely on your local machine.**

---

## How It Works

1. You write a `prd.json` with user stories (title, description, files, acceptance criteria)
2. Ralph calls your local LLM in a loop, giving it tools: `read_file`, `write_file`, `run_command`
3. After each story, Ralph runs the acceptance criteria and quality checks — pass → next story, fail → retry (up to 5x)
4. Telegram notifications on story complete, story blocked, and all-done
5. Each story runs in a **fresh subprocess** — clean memory isolation, no state leakage

---

## Requirements

- Python 3.10+
- [llama.cpp](https://github.com/ggerganov/llama.cpp) (`brew install llama.cpp` on macOS)
- A Qwen3.5 GGUF model (see **Model** section below)
- `pip install pyyaml requests`

---

## Model

Ralph is tested and optimized for **Qwen3.5-35B-A3B** (MoE, ~20GB Q4_K_M) via `llama-server` from llama.cpp.

### Backend history

| Version | Backend | Model | Notes |
|---------|---------|-------|-------|
| v0.1 | MLX vllm-mlx | qwen3.5:27b instruct | Original. Slow. |
| v0.2 | Ollama | qwen3.5:35b MoE | 3.4× faster than MLX. |
| v0.3+ | llama-server | Qwen3.5-35B-A3B-Q4_K_M.gguf | **Current.** More stable than Ollama — no KV pre-allocation crashes. |

### Why llama-server over Ollama?

Ollama pre-allocates the full KV cache per request (~12GB+ for large contexts), causing crashes under memory pressure on Apple Silicon. llama-server uses a fixed `--ctx-size` with no dynamic allocation — predictable and crash-free.

### Downloading the model

```bash
# HuggingFace CLI
pip install huggingface_hub
huggingface-cli download bartowski/Qwen_Qwen3.5-35B-A3B-GGUF \
  Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf \
  --local-dir ~/llama-models/
```

### Starting llama-server

```bash
# 35B — surgical edits, large existing files
llama-server \
  --model ~/llama-models/Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf \
  --port 11434 \
  --ctx-size 131072 \
  --flash-attn on \
  --jinja \
  -ngl 99

# 122B — new file creation, large-file stories (run on separate port, not simultaneously with 35B)
llama-server \
  --model ~/llama-models/Qwen_Qwen3.5-122B-A10B-Q4_K_M.gguf \
  --port 11435 \
  --ctx-size 131072 \
  --flash-attn on \
  --jinja \
  -ngl 99
```

`-ngl 99` offloads all layers to GPU (Metal on Apple Silicon, or adjust for your GPU VRAM). `--flash-attn on` is required — the bare `--flash-attn` flag is not accepted in llama.cpp ≥ b5000. `--jinja` is required for reliable tool calls — do not omit it.

### Confirmed model compatibility

| Model | Works? | Notes |
|-------|--------|-------|
| `Qwen3.5-35B-A3B-Q4_K_M.gguf` | ✅ Yes | Primary tested model. Fast (~5-8 tok/s on CPU). Best for edits to large existing files. Use `--ctx-size 131072`. |
| `Qwen3.5-122B-A10B-Q4_K_M.gguf` | ✅ Yes | Stable at scale with large context window. Best for new file creation and large-file stories. Use `--ctx-size 131072`. ~3× slower than 35B. |
| `qwen3.5:35b` (Ollama) | ⚠️ Partial | Works but less stable than llama-server for tool calls. |
| Other OpenAI-compat endpoints | ❓ Untested | Must support tool calls. |

> **Note on 35B vs 122B:** Both models work well — they serve different use cases. Run them as **separate llama-server instances on different ports** but never simultaneously (they compete for memory). Serialize runs.

### Why llama-server over Ollama for tool calls?

Beyond the KV cache stability improvement, llama-server handles tool calls more reliably than Ollama. Ollama can silently drop `--jinja` flag behavior; llama-server with `--jinja` produces consistent, well-formed tool call JSON across both model sizes.

### Known Qwen3.5 quirks (handled automatically)

- **Thinking model:** Ralph disables thinking via `chat_template_kwargs: {"enable_thinking": false}` (llama-server syntax). Do NOT use `"thinking": {"type": "disabled"}` — it is silently ignored. Without disabling, the model fills context with `<think>` blocks and stops after `<tool_calls>`.
- **Em-dash generation:** The model occasionally generates Unicode em-dashes and curly quotes in Python code. Ralph's `write_file` strips all non-ASCII from `.py` files before writing.
- **`<tool_calls>` truncation:** On large contexts, the model may emit `finish=stop` immediately after opening `<tool_calls>`. Ralph detects and retries.
- **`--jinja` is required:** Without it, tool calls are silently malformed. Always include `--jinja` in your llama-server launch command.

---

## Hardware

Tested on **Apple Silicon Mac mini M4 (64GB unified memory)**. The 64GB is important — the Q4_K_M model loads at ~20GB, leaving headroom for the 65K KV cache during long coding sessions.

Minimum recommended: **32GB RAM**. 64GB gives comfortable headroom.

---

## Setup

```bash
git clone https://github.com/blockops1/ralph-loop-local-llm
cd ralph-loop-local-llm

# Install deps
pip install pyyaml requests

# Copy and edit config
cp config.example.yaml config.yaml
# Edit config.yaml: set model_url and model_id to match your llama-server setup

# Verify llama-server is running
curl http://localhost:11434/v1/models | python3 -m json.tool

# Set up Telegram notifications (optional but recommended)
echo "TELEGRAM_BOT_TOKEN=your_token" >> ~/.env
echo "TELEGRAM_CHAT_ID=your_chat_id" >> ~/.env
```

**Telegram setup:** Create a bot via [@BotFather](https://t.me/BotFather), get your chat ID via [@userinfobot](https://t.me/userinfobot).

---

## Config

```yaml
# config.yaml
model_url: "http://localhost:11434/v1"        # llama-server OpenAI-compat endpoint
model_id: "Qwen3.5-35B-A3B-Q4_K_M.gguf"     # Must match model filename (without path)

# Loop limits
max_iterations: 20            # Max stories per run
max_attempts_per_story: 5     # Retries before marking BLOCKED
max_tool_calls_per_story: 160

# Model generation
max_tokens: 16384             # Allow large completions — no cost penalty for local models
max_context_tokens: 60000     # Align with llama-server --ctx-size (leave ~10% headroom)

# Timeouts (seconds)
request_timeout: 14400        # 4 hours — large context prefill can be slow on first run
quality_check_timeout: 120    # Max time for acceptance criteria commands

# Notifications
notify_on_story_complete: true
notify_on_story_fail: true
notify_on_all_complete: true
notify_on_blocked: true       # Alert when a story exhausts max_attempts_per_story
```

---

## Usage

```bash
# Run a specific project
./ralph.sh my-project

# Auto-pick first pending project (via loop_runner.py)
./ralph.sh

# Lint a PRD before running (recommended)
python3 prd_linter.py projects/my-project/prd.json
```

Ralph writes a `progress.txt` in the project directory and sends Telegram notifications. Check `logs/` for the full run log.

---

## Writing a PRD

Create `projects/my-project/prd.json`. The full schema:

```json
{
    "project": "my-project",
    "slug": "my-project",
    "description": "What this project builds",
    "contextFiles": [
        "scripts/main.py"
    ],
    "qualityChecks": "python3 -m py_compile scripts/main.py",
    "userStories": [
        {
            "id": "US-001",
            "title": "Add error handling to fetcher.py",
            "description": "In scripts/fetcher.py, wrap the requests.get() call in a try/except block. On ConnectionError, log the error and return None. Do not change any other logic.",
            "acceptanceCriteria": [
                "python3 -m py_compile scripts/fetcher.py",
                "grep -q 'ConnectionError' scripts/fetcher.py"
            ],
            "contextFiles": [
                "scripts/fetcher.py"
            ],
            "qualityChecks": "python3 -m py_compile scripts/fetcher.py",
            "priority": 1,
            "passes": false,
            "attempts": 0,
            "lastAttempt": null,
            "error": null,
            "notes": "",
            "dependsOn": [],
            "dependencyPolicy": "skip"
        }
    ]
}
```

**Schema notes:**
- `contextFiles` — files Ralph pre-loads into the system prompt before starting each story. Ralph sees the full file content without making a `read_file` call. Always include the file being modified.
- `qualityChecks` — shell command(s) run after `task_complete` as a final gate. Can be a string or list. Must exit 0 to pass.
- `acceptanceCriteria` — list of shell commands Ralph runs itself to verify its work. Must exit 0 to pass.
- `dependsOn` — list of story IDs. If a dependency failed, this story is skipped (or blocks all, per `dependencyPolicy`).
- `passes` / `attempts` — managed by Ralph. Set both to `false`/`0` before running.

### Lint before running

```bash
python3 prd_linter.py projects/my-project/prd.json
```

The linter catches missing required fields, invalid status values, circular `dependsOn` references, and malformed `contextFiles` paths before you waste a run on a bad PRD.

---

## Tips for Reliable Stories

**The most important rule:** validate your `acceptanceCriteria` commands manually before launching Ralph. If they fail on already-correct code, Ralph will loop forever.

```bash
# Pre-flight check — run every AC command manually
python3 -m py_compile scripts/myfile.py
grep -q 'def my_function' scripts/myfile.py
```

**Other tips:**
- **One story = one focused change** — don't bundle multiple features
- **Name the exact file and function** — don't say "update the parser", say "in `parser.py`, find `parse_line()` and add X"
- **Always add the modified file to `contextFiles`** — Ralph pre-loads it; without it Ralph reads fresh and may overwrite your changes
- **Use `dependsOn`** for stories that build on each other — prevents running US-002 if US-001 failed
- **ASCII only in descriptions** — avoid em-dashes, curly quotes, or special Unicode. Ralph sanitizes `.py` writes but clean descriptions help too.
- **Pre-create target files** — stories that modify existing files are more reliable than stories that create new ones from scratch
- **Concrete descriptions beat abstract ones** — "add a `get_balance()` function that calls `requests.get(URL + '/balance')` and returns `response.json()['balance']`" is better than "add a balance getter"

---

## Project Structure

```
ralph/
├── ralph.py              # Core loop + LLM client
├── tools.py              # File/shell tool implementations
├── prd_manager.py        # PRD loading, story state management
├── prd_linter.py         # PRD validator — run before launching Ralph
├── loop_runner.py        # Subprocess story runner + auto-loop across projects
├── notify_watcher.py     # Telegram notification watcher (run alongside ralph.sh)
├── ralph.sh              # Shell wrapper — always use this to run Ralph
├── PROMPT.md             # System prompt sent to the model each turn
├── config.yaml           # Your local config (gitignored)
├── config.example.yaml   # Template — copy to config.yaml
└── projects/
    ├── example/          # Working 2-story example PRD
    └── your-project/     # Your PRDs go here
```

**Always run Ralph via `ralph.sh`**, not `python3 ralph.py` directly. The shell wrapper sources your env file (for Telegram tokens) and handles the process correctly.

---

## Story Lifecycle & BLOCKED status

Each story goes through these states:

| Status | Meaning |
|--------|---------|
| `pending` | Not yet attempted |
| `in_progress` | Currently running |
| `done` | Passed all acceptance criteria |
| `failed` | Failed last attempt (will retry) |
| `blocked` | Exhausted `max_attempts_per_story` — terminal, skipped on future runs |

When a story is **BLOCKED**, Ralph:
1. Sets `status: "blocked"` in `prd.json`
2. Sends a Telegram alert with the story ID and last error
3. Continues to the next unblocked story

To unblock a story: fix the PRD, reset `status` to `pending` and `attempts` to `0`, then rerun.

---

## Scheduling (optional)

To run Ralph overnight, add a launchd plist or cron job pointing to `ralph.sh`. Ralph is safe to schedule — it acquires a lockfile, checks for pending stories, and exits cleanly if nothing is pending or another instance is running.

---

## Intervention

If a story is BLOCKED, Ralph sends a Telegram alert. To intervene:

```bash
# Check progress
cat projects/my-project/progress.txt

# Check the full run log
tail -100 logs/my-project-YYYY-MM-DD.log

# Fix the PRD (description, acceptanceCriteria, or contextFiles)
# Then reset the story:
python3 -c "
import json
data = json.load(open('projects/my-project/prd.json'))
for s in data['userStories']:
    if s['id'] == 'US-003':
        s['status'] = 'pending'
        s['attempts'] = 0
        s['error'] = None
open('projects/my-project/prd.json', 'w').write(json.dumps(data, indent=2))
"

# Remove any stale lockfile
rm -f projects/my-project/.ralph.lock

# Lint before rerunning
python3 prd_linter.py projects/my-project/prd.json

# Relaunch
./ralph.sh my-project
```

---

## License

MIT
