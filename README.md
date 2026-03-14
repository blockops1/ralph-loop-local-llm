# Ralph Loop — Autonomous Local LLM Coding Agent

Ralph is a lightweight autonomous coding loop that reads a Product Requirements Document (PRD), executes user stories one at a time using a local LLM, verifies each story with acceptance tests, and notifies you via Telegram when done.

**No cloud. No API costs. Runs entirely on your local machine.**

---

## How It Works

1. You write a `prd.json` with user stories (title, description, files, acceptance criteria)
2. Ralph calls your local LLM in a loop, giving it tools: `read_file`, `write_file`, `run_command`
3. After each story, Ralph runs the acceptance tests — pass → next story, fail → retry (up to 3x)
4. Telegram notifications on story complete, story fail, and all-done

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally
- A model pulled in Ollama (see **Model** section below)
- `pip install pyyaml requests`

---

## Model

Ralph was benchmarked with **Qwen3.5:35b** — a sparse MoE model that runs fast on Apple Silicon (64GB recommended):

```bash
ollama pull qwen3.5:35b
```

Benchmark results (3-story fixed benchmark, Mac mini M4 64GB):

| Model | Stories | Time |
|-------|---------|------|
| qwen3.5:27b instruct | 3/3 ✅ | ~11 min |
| qwen3.5:35b MoE | 3/3 ✅ | ~3 min |

The MoE architecture activates only ~10B parameters per token, giving 3.4× speedup with no quality loss.

Any OpenAI-compatible model endpoint works — just update `config.yaml`.

---

## Setup

```bash
git clone https://github.com/blockops1/ralph-loop-local-llm
cd ralph-loop-local-llm

# Install deps
pip install pyyaml requests

# Copy and edit config
cp config.example.yaml config.yaml
# Edit config.yaml: set model_url and model_id

# Set up Telegram notifications (optional)
echo "TELEGRAM_BOT_TOKEN=your_token" >> ~/.openclaw/.env
echo "TELEGRAM_CHAT_ID=your_chat_id" >> ~/.openclaw/.env
```

---

## Config

```yaml
# config.yaml
model_url: "http://localhost:11434/v1"   # Ollama OpenAI-compat endpoint
model_id: "qwen3.5:35b"                 # Model to use

max_iterations: 20          # Max stories per run
max_attempts_per_story: 3   # Retries before marking failed
max_tool_calls_per_story: 160

max_tokens: 16384
max_context_tokens: 262000
request_timeout: 14400      # 4 hours — local models can be slow on large context

notify_on_story_complete: true
notify_on_story_fail: true
notify_on_all_complete: true
```

---

## Usage

```bash
# Auto-pick first active project
./ralph.sh

# Run a specific project
./ralph.sh my-project

# Dry run (no LLM calls)
./ralph.sh my-project --dry-run
```

---

## Writing a PRD

Create `projects/my-project/prd.json`:

```json
{
    "name": "my-project",
    "description": "What this project builds",
    "version": "1.0",
    "userStories": [
        {
            "id": "US-001",
            "title": "Add error handling to fetcher.py",
            "description": "In scripts/fetcher.py, wrap the requests.get() call in a try/except. On ConnectionError, print 'Connection failed' and return None.",
            "files": ["scripts/fetcher.py"],
            "acceptance": [
                "python3 -m py_compile scripts/fetcher.py && echo PASS || echo FAIL",
                "grep -q 'ConnectionError' scripts/fetcher.py && echo PASS || echo FAIL"
            ],
            "status": "pending"
        }
    ]
}
```

See `projects/benchmark/prd.json` for a working 3-story example.

---

## Tips for Good Stories

- **One story = one focused change** — don't bundle multiple features
- **Acceptance tests must be shell commands** that print `PASS` or `FAIL`
- **Name the exact file and function** — don't say "update the parser", say "in `parser.py`, find `parse_line()`"
- **Pre-create target files** — Ralph can create new files, but stories are more reliable when the file already exists
- **Validate acceptance criteria manually** before running Ralph — if they fail on already-correct code, Ralph will loop forever

---

## Project Structure

```
ralph/
├── ralph.py              # Core loop + LLM client
├── tools.py              # File/shell tool implementations
├── prd_manager.py        # PRD loading, story state management
├── loop_runner.py        # Auto-loop across multiple projects
├── notify_watcher.py     # Telegram notification watcher
├── ralph.sh              # Shell wrapper (use this to run Ralph)
├── config.yaml           # Your local config (gitignored)
├── config.example.yaml   # Template
├── PROMPT.md             # System prompt Ralph uses
└── projects/
    └── benchmark/        # Example 3-story benchmark PRD
```

---

## Notifications (Telegram)

Ralph sends Telegram messages on story complete/fail and when all stories are done.

Set up a bot via [@BotFather](https://t.me/BotFather), get your chat ID, and add to your env file:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Ralph reads from `~/.openclaw/.env` by default. To use a different path, edit `ralph.py`.

---

## License

MIT
