# Changelog

## v0.4.0 — 2026-03-19

### New: subprocess story runner (loop_runner.py)

Each story now runs in a fresh subprocess. This gives clean memory isolation between stories — no state leakage from a crashed or stuck story into the next one. `loop_runner.py` spawns `ralph.py --single-story` per story, reads the result from a sidecar JSON file, and continues the loop.

### New: prd_linter.py

A standalone PRD linter that validates `prd.json` before running. Catches common errors:
- Missing required fields (`id`, `title`, `acceptanceCriteria`)
- Invalid status values
- Circular `dependsOn` references
- Malformed `contextFiles` paths

Run standalone: `python3 prd_linter.py path/to/prd.json`

### New: contextFiles pre-loaded into system prompt

Files listed in `contextFiles` are now read and injected directly into the story prompt at start. Ralph sees the file contents without needing a `read_file` call — eliminates re-read loops on files that get pushed out of working context. Files that don't exist yet show a `(create it)` placeholder so Ralph knows to create them.

### Improved: notify_watcher.py uses Telegram Bot API directly

Previously used the `openclaw` CLI (non-portable). Now uses `requests` + Telegram Bot API directly. Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars — no OpenClaw dependency.

### Improved: read_file truncation limit raised 500 → 2000 lines

Large files no longer get aggressively truncated. Aligns with llama-server's 65k context window.

### Bug fixes

- `load_config()` accepts optional `config_path` argument — useful for testing alternate configs
- PRD story_summary handles missing `title` field gracefully
- `notify_watcher.py` sends each line as a separate message instead of batching (cleaner on mobile)

---

## v0.3.0 — 2026-03-17

### Switched from Ollama to llama.cpp (llama-server)

Ollama was causing memory pressure crashes under load (pre-allocates full KV cache per request regardless of actual message size). Switched to `llama-server` from `llama.cpp`:

| Backend | Model | Stability |
|---------|-------|-----------|
| Ollama | qwen3.5:35b MoE | Crashes under memory pressure (30GB+ alloc) |
| llama-server | Qwen3.5-35B-A3B-Q4_K_M.gguf | Stable — fixed ctx-size, no dynamic alloc |

**Setup:** `brew install llama.cpp` then run llama-server with `--ctx-size 65536 --flash-attn on --jinja`.

**Note:** llama.cpp uses different no-think syntax than Ollama:
- Ollama: `"options": {"think": False}`
- llama.cpp: `"chat_template_kwargs": {"enable_thinking": False}`

### New: write_file truncation guard

`tools.py` now rejects `write_file` calls that contain truncation artifacts. If the model internally truncates a large file and writes back `... [truncated - N total lines]` as literal content, `write_file` returns an error and the model must re-read and write the complete content.

Patterns detected: `"... [truncated"`, `"[truncated -"`, `"# ... truncated"`, `"# [truncated"`.

### New: sequential ralph.sh (blocks on PID)

`ralph.sh` now waits for Ralph to finish before returning (`wait $RALPH_PID`). This makes sequential chaining safe — just run `ralph.sh slug-1`, `ralph.sh slug-2`, etc. and they execute in order. Previously, ralph.sh returned immediately, causing parallel llama-server deadlocks when scripts chained multiple PRDs.

### New: BLOCKED story status with rich Telegram alert

When a story exhausts `max_attempts_per_story`, Ralph now:
1. Sets `status: "blocked"` in prd.json (terminal state — Ralph skips it on future runs)
2. Sends a rich Telegram notification with story ID, project slug, and last error
3. Continues to the next unblocked story

Previously: silent failure at max attempts. Now: explicit BLOCKED state + alert.

### New: pre-write self-review checklist in PROMPT.md

Before calling `write_file`, Ralph now outputs a structured checklist:
- Every function/method being added or changed
- Every database table name and column name referenced in SQL
- Every external API endpoint or attribute being called
- How each acceptance criterion will be satisfied

This catches schema hallucinations (wrong table/column names) at near-zero cost — same inference call, more structured output.

### New: contextFiles pre-loaded into system prompt

Files listed in `contextFiles` are now pre-loaded directly into the system prompt at story start. The model sees the file contents before making any tool calls — eliminating re-read loops on large files that were getting pushed out of working context.

Context budget note: pre-loaded files count against `max_context_tokens`. Align this with llama-server `--ctx-size` (leave ~10% headroom for generation).

### Configuration changes

| Setting | Old | New | Reason |
|---------|-----|-----|--------|
| `max_context_tokens` | 262000 | 60000 | Align with llama-server 65536 ctx |
| `max_tool_output_chars` | 32000 | 80000 | Large files come back whole |
| `max_attempts_per_story` | 3 | 5 | More runway before BLOCKED |
| `request_timeout` | 7200 | 14400 | 4h for large-context cold prefill |

### Bug fixes

- **Loop detector threshold raised** — `max_recent_calls` 10→20, `repetition_threshold` 3→6. Previously fired too aggressively on large-file stories that required multiple reads before writing.
- **VERSION file** — synced to match CHANGELOG (was stuck at 0.1.0).

---

## v0.2.0 — 2026-03-15

### Switched from MLX to Ollama

Ralph originally ran on the MLX inference stack. After benchmark testing (3-story fixed benchmark, Mac mini M4 64GB), we switched to Ollama:

| Backend | Model | Time |
|---------|-------|------|
| MLX (waybarrios vllm-mlx) | qwen3.5:27b instruct | ~11 min |
| Ollama | qwen3.5:35b MoE | ~3 min |

**3.4× speedup** with no quality loss.

### Bug fixes

- **`qualityChecks` string vs list normalization** — now normalized to a list before running.
- **write_file non-ASCII sanitization** — strips em-dashes, curly quotes from `.py` files.
- **git_commit non-ASCII sanitization** — same fix for commit messages.
- **`think=False` in Ollama options** — disabled by default (Qwen3.5 thinking model).
- **`<think>` block stripping** — even with `think=False`, sometimes emitted; now stripped.
- **Bare `<tool_calls>` truncation recovery** — detects and retries on `finish=stop` after opening tag.
- **Destructive git subcommands blocked** — `git checkout`, `git reset`, etc. rejected in `run_command`.
- **Dynamic `num_ctx`** — injected per-request based on actual message size.

### PRD schema additions

- `contextFiles` — list of files to pre-load before the story runs.
- `dependsOn` / `dependencyPolicy` — story dependency tracking.

---

## v0.1.0 — 2026-03-10

Initial release. Core autonomous loop with MLX backend, Telegram notifications, PRD-driven story execution, acceptance criteria verification, and git integration.
