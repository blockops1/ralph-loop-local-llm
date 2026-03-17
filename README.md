# Ralph Loop — Autonomous Local LLM Coding Agent

Ralph is a lightweight autonomous coding loop that reads a Product Requirements Document (PRD), executes user stories one at a time using a local LLM, verifies each story with acceptance tests, and notifies you via Telegram when done.

**No cloud. No API costs. Runs entirely on your local machine.**

---

## How It Works

1. You write a `prd.json` with user stories (title, description, files, acceptance criteria)
2. Ralph calls your local LLM in a loop, giving it tools: `read_file`, `write_file`, `run_command`
3. After each story, Ralph runs the acceptance criteria and quality checks — pass → next story, fail → retry (up to 3x)
4. Telegram notifications on story complete, story fail, and all-done

---

## Requirements

- Python 3.10+
- [llama.cpp](https://github.com/ggerganov/llama.cpp) (`brew install llama.cpp` on macOS)
- A Qwen3.5 GGUF model (see **Model** section below)
- `pip install pyyaml requests`

> **Note:** Ralph v0.2 used Ollama. v0.3 switched to `llama-server` from llama.cpp for better memory stability on Apple Silicon. Ollama pre-allocates the full KV cache per request (~12GB+ for large contexts), causing crashes under load. llama.cpp uses a fixed `--ctx-size` with no dynamic allocation.

---

## Model

Ralph is tested and benchmarked with **Qwen3.5:35b** via Ollama. This is the only currently confirmed working configuration.

```bash
ollama pull qwen3.5:35b
```

### Why Ollama over MLX?

Ralph originally ran on the MLX inference stack. After benchmark testing, we switched to Ollama for significantly better performance:

| Backend | Model | Stories | Time |
|---------|-------|---------|------|
| MLX (waybarrios vllm-mlx) | qwen3.5:27b instruct | 3/3 ✅ | ~11 min |
| Ollama | qwen3.5:35b MoE | 3/3 ✅ | ~3 min |

The Ollama qwen3.5:35b MoE model activates only ~10B parameters per token, giving a **3.4× speedup** with no quality loss compared to the larger instruct model. Ollama also handles model switching automatically — no server restarts needed.

### Confirmed model compatibility

| Model | Works? | Notes |
|-------|--------|-------|
| `qwen3.5:35b` | ✅ Yes | Primary tested model. Fast, reliable tool calls. |
| `qwen3.5:27b` | ✅ Yes | Slower (~3.4×). Works but not recommended. |
| `qwen3.5:122b` | ⚠️ Partial | Hits `finish=stop` truncation after `<tool_calls>` — mitigation in place but unreliable. Not recommended for production use. |
| Other Ollama models | ❓ Untested | Must support tool calls. OpenAI-compatible endpoint required. |

### Known Qwen3.5 quirks (handled automatically)

Ralph handles these internally — you don't need to configure anything:

- **Thinking model behavior:** Qwen3.5 is a thinking model. Ralph disables thinking via `chat_template_kwargs: {"enable_thinking": False}` (llama.cpp syntax). Without this, the model fills the context with `<think>` blocks then emits `<tool_calls>` and stops (`finish=stop` with 2 tokens). Note: Ollama used `"options": {"think": False}` -- different syntax.
- **Em-dash generation:** The model occasionally generates Unicode em-dashes (—) and curly quotes in Python code it writes, causing `SyntaxError`. Ralph's `write_file` tool automatically strips all non-ASCII characters from `.py` files before writing to disk.
- **`<tool_calls>` truncation:** On large context windows or with certain models, Ollama may emit `finish=stop` immediately after opening `<tool_calls>`, before the JSON body. Ralph detects this and retries the call with a clean history.
- **`qualityChecks` format:** The `qualityChecks` field in `prd.json` can be either a string (single command) or a list. Ralph handles both.

---

## Hardware

Tested on **Apple Silicon Mac mini M4 (64GB unified memory)**. The 64GB is important — `qwen3.5:35b` loads at ~23GB, leaving headroom for the KV cache during long coding sessions.

Ralph also runs on Linux + Ollama (tested on a server with Nvidia GPU). The 122b model works on a high-VRAM Linux server but has the truncation issues noted above.

Minimum recommended: **32GB RAM** for qwen3.5:35b. 64GB gives comfortable headroom.

---

## Setup

```bash
git clone https://github.com/blockops1/ralph-loop-mlx-qwen35
cd ralph-loop-mlx-qwen35

# Install deps
pip install pyyaml requests

# Copy and edit config
cp config.example.yaml config.yaml
# Edit config.yaml: verify model_url and model_id match your Ollama setup

# Verify Ollama is running and model is available
curl http://localhost:11434/api/tags | python3 -m json.tool | grep qwen

# Set up Telegram notifications (optional but recommended)
echo "TELEGRAM_BOT_TOKEN=your_token" >> ~/.env
echo "TELEGRAM_CHAT_ID=your_chat_id" >> ~/.env
```

**Telegram setup:** Create a bot via [@BotFather](https://t.me/BotFather), get your chat ID via [@userinfobot](https://t.me/userinfobot). Ralph reads from `~/.env` by default (fallback: `~/.openclaw/.env`) — edit the `notify()` function in `ralph.py` to change the path.

---

## Config

```yaml
# config.yaml
model_url: "http://localhost:11434/v1"   # llama-server OpenAI-compat endpoint
model_id: "Qwen3.5-35B-A3B-Q4_K_M.gguf" # Must match model filename (without path)

# Loop limits
max_iterations: 20          # Max stories per run
max_attempts_per_story: 3   # Retries before marking failed
max_tool_calls_per_story: 160

# Model generation
max_tokens: 16384           # Allow large completions — no cost penalty for local models
max_context_tokens: 262000  # Full 262K context window

# Timeouts (seconds)
request_timeout: 14400      # 4 hours — large context prefill can be slow
quality_check_timeout: 120  # Max time for acceptance criteria commands

# Notifications
notify_on_story_complete: true
notify_on_story_fail: true
notify_on_all_complete: true
notify_on_blocked: true
```

---

## Usage

```bash
# Run a specific project
./ralph.sh my-project

# Auto-pick first pending project (via loop_runner.py)
./ralph.sh

# Dry run (no LLM calls, just validates PRD)
./ralph.sh my-project --dry-run
```

Ralph writes a `progress.txt` in the project directory and sends Telegram notifications. Check `ralph/logs/` for the full run log.

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
- `contextFiles` — files Ralph pre-loads before working on this story. Always include the file being modified.
- `qualityChecks` — shell command(s) run after `task_complete` as a final gate. Can be a string or list. Must exit 0 to pass.
- `acceptanceCriteria` — list of shell commands Ralph runs itself to verify its work. Must exit 0 to pass.
- `dependsOn` — list of story IDs. If a dependency failed, this story is skipped (or blocks all, per `dependencyPolicy`).
- `passes` / `attempts` — managed by Ralph. Set both to `false`/`0` before running.

See `projects/example/prd.json` for a working 2-story example.

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
- **Always add the modified file to `contextFiles`** — without it, Ralph may read it fresh and overwrite your changes
- **Use `dependsOn`** for stories that build on each other — prevents running US-002 if US-001 failed
- **ASCII only in descriptions** — avoid em-dashes, curly quotes, or special Unicode in story text. The model may reproduce them in generated code, causing SyntaxErrors. Ralph sanitizes `.py` writes but clean descriptions help too.
- **Pre-create target files** — stories that modify existing files are more reliable than stories that create new ones from scratch
- **Concrete descriptions beat abstract ones** — "add a `get_balance()` function that calls `requests.get(URL + '/balance')` and returns `response.json()['balance']`" is better than "add a balance getter"

---

## Project Structure

```
ralph/
├── ralph.py              # Core loop + LLM client
├── tools.py              # File/shell tool implementations
├── prd_manager.py        # PRD loading, story state management
├── loop_runner.py        # Auto-loop across multiple projects (for cron)
├── notify_watcher.py     # Telegram notification helper
├── ralph.sh              # Shell wrapper — always use this to run Ralph
├── PROMPT.md             # System prompt sent to the model each turn
├── config.yaml           # Your local config (gitignored)
├── config.example.yaml   # Template — copy to config.yaml
└── projects/
    ├── example/          # Working 2-story example PRD
    └── your-project/     # Your PRDs go here
```

**Always run Ralph via `ralph.sh`**, not `python3 ralph.py` directly. The shell wrapper sets the CPU governor, sources your env file (for Telegram tokens), and handles the process correctly.

---

## Scheduling (optional)

To run Ralph on a schedule (e.g., overnight), add a cron job or launchd plist pointing to `ralph.sh`. Ralph is safe to run on a schedule — it acquires a lockfile, checks for pending stories, and exits cleanly if nothing is pending or another instance is running.

---

## Intervention

If a story fails 3 times, Ralph sends a Telegram alert and stops. To intervene:

```bash
# Check progress
cat projects/my-project/progress.txt

# Check the full run log
tail -100 ralph/logs/my-project-YYYY-MM-DD.log

# Fix the PRD (description, acceptanceCriteria, or contextFiles)
# Reset the story
python3 -c "
import json
data = json.load(open('projects/my-project/prd.json'))
for s in data['userStories']:
    if s['id'] == 'US-003':
        s['passes'] = False
        s['attempts'] = 0
open('projects/my-project/prd.json', 'w').write(json.dumps(data, indent=2))
"

# Remove any stale lockfile
rm -f projects/my-project/.ralph.lock

# Relaunch
./ralph.sh my-project
```

---

## License

MIT
