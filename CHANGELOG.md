# Changelog

## v0.2.0 — 2026-03-15

### Switched from MLX to Ollama

Ralph originally ran on the MLX inference stack. After benchmark testing (3-story fixed benchmark, Mac mini M4 64GB), we switched to Ollama:

| Backend | Model | Time |
|---------|-------|------|
| MLX (waybarrios vllm-mlx) | qwen3.5:27b instruct | ~11 min |
| Ollama | qwen3.5:35b MoE | ~3 min |

**3.4× speedup** with no quality loss. Ollama also handles model switching without server restarts.

### Bug fixes

- **`qualityChecks` string vs list normalization** — `prd.json` `qualityChecks` can now be a string (single command) or a list. Previously, a string value was iterated character-by-character, causing the quality check command to be truncated to a single character (`p`), which always failed. Now normalized to a list before running.

- **write_file non-ASCII sanitization** — Qwen3.5 occasionally generates Unicode em-dashes (—), curly quotes, and arrows in Python code, causing `SyntaxError` on write. `write_file` now automatically strips all non-ASCII characters from `.py` files before writing to disk.

- **git_commit non-ASCII sanitization** — Same fix applied to commit messages. The model occasionally generated em-dashes in commit message text.

- **`think=False` in Ollama options** — Qwen3.5 is a thinking model. Without `think=False`, it fills the context with `<think>` reasoning blocks, then stops after opening `<tool_calls>` before emitting the JSON body (`finish=stop` with 2 tokens). Now disabled by default.

- **`<think>` block stripping** — Even with `think=False`, Qwen3.5 sometimes emits `<think>` blocks. These are now stripped from model responses before parsing.

- **Bare `<tool_calls>` truncation recovery** — If the model emits `finish=stop` immediately after `<tool_calls>` (without the JSON body), Ralph now detects this, discards the partial response without adding it to history, and retries the call clean. Up to 1 retry with a skip-plan nudge.

- **Destructive git subcommands blocked** — `run_command` now rejects `git checkout`, `git reset`, `git revert`, `git clean`, `git stash`, and `git restore`. These were being triggered by the model when a quality check failed and it tried to revert its own changes, causing an unrecoverable loop. The model now gets an error message and must repair the file instead.

- **Dynamic `num_ctx`** — Ollama pre-allocates the full KV cache based on `num_ctx`. With a 262K context window, this was allocating ~12GB of RAM for every request regardless of actual message size. Ralph now calculates the actual message size and injects a matching `num_ctx` per request (floor 8192, ceiling 131072), reducing memory pressure significantly.

### PRD schema additions

- `contextFiles` — list of files to pre-load before the story runs. Strongly recommended for any story that modifies an existing file.
- `qualityChecks` — now accepts both string and list formats.
- `dependsOn` / `dependencyPolicy` — story dependency tracking. If a dependency failed, the dependent story is skipped or blocks all remaining stories.

### Documentation

- README rewritten: Ollama setup, confirmed model matrix, Qwen3.5 quirk reference, updated PRD schema, intervention guide
- Example PRD updated

---

## v0.1.0 — 2026-03-10

Initial release. Core autonomous loop with MLX backend, Telegram notifications, PRD-driven story execution, acceptance criteria verification, and git integration.
