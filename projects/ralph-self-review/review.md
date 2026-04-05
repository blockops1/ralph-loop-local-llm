# Code Review: ralph.py
**Date:** 2026-03-18T12:00:00
**Reviewer:** Ralph Review Agent
**Files reviewed:** 1
**Stories in PRD:** 1
**Overall:** ⚠️ Fix first

---

## Summary
ralph.py is the main orchestrator for the Ralph autonomous coding loop. It reads PRDs, runs agentic loops with a local LLM, executes tool calls, runs quality checks, and manages story completion. The codebase is functional but has several bugs that could cause silent failures, incorrect behavior, or crashes in production.

---

## 🔴 Critical (must fix before ship)
| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | ralph.py:374 | `tool_call_count += 1` is incremented when model returns no tool calls but `finish_reason in ("stop", "length")` - this counts as a tool call even though no tool was executed, causing the loop to exit prematurely after ~160 such "nudge" cycles instead of actual tool calls. | Stories can fail after 160 model nudges even if the model never made a real tool call, wasting context and time. | Only increment `tool_call_count` when actual tool calls are executed, not on nudge cycles. Move increment inside the `if tool_calls:` block. |
| 2 | ralph.py:427 | `completion_summary = args.get("summary", "")` - when `task_complete` is called, the summary is extracted from args, but if args is malformed JSON or missing "summary", this silently sets an empty string. The code then treats empty string as success (`completion_summary.startswith("FAILED:")` is False). | Failed task completions with empty/missing summary are treated as success, causing stories to be marked done incorrectly. | Validate that summary is non-empty before treating as success. Add: `if not completion_summary: log.warning("task_complete called with empty summary"); return False, "task_complete called without summary"` |
| 3 | ralph.py:485 | `tool_run_command` is imported inside the function with `from tools import tool_run_command, WORKSPACE` - but `tool_run_command` does not exist in tools.py (only `execute_tool` exists which dispatches to tools). | Quality checks will always fail with AttributeError, preventing any story from passing quality checks. | Use `execute_tool("run_command", {"command": check, "timeout": 60})` or import the actual function name from tools.py. |
| 4 | ralph.py:518 | Telegram notification uses hardcoded `chat_id = "374999219"` - if this ID changes or is wrong, notifications silently fail (only logs warning). Also, the message format `f"_ Ralph: {msg}"` uses Markdown italics prefix `_` which may not render correctly. | Notifications may fail silently or format incorrectly, reducing operational visibility. | Make chat_id configurable via config.yaml or environment variable. Fix message format to not use leading underscore. |

---

## ⚠️ Medium (fix before next sprint)
| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | ralph.py:138 | `estimate_tokens(text: str) -> int: return len(text) // 4` - this is a very rough estimate that assumes 4 chars per token, which is inaccurate for most text (English averages ~4 chars/token but code, JSON, etc. vary widely). | Use a more accurate estimation method or add a comment that this is a rough heuristic. Consider using `len(text.split()) * 1.3` for better accuracy. |
| 2 | ralph.py:175 | `parse_sse_to_completion` is defined but never used - `call_model` has its own SSE parsing logic that duplicates this functionality. | Remove unused function or refactor to share code between the two SSE parsers. |
| 3 | ralph.py:295 | `call_model_with_heartbeat` is a passthrough that just calls `call_model` - the comment says "streaming now provides live visibility; no separate heartbeat needed" but the function adds no value. | Remove this wrapper function to reduce code complexity. |
| 4 | ralph.py:352 | Loop detection uses `recent_calls.count(call_signature)` which is O(n) for each call, making loop detection O(n²) in the worst case. With `max_recent_calls=20` this is negligible but could be optimized. | Use a Counter or dict to track call frequencies for O(1) lookups. |
| 5 | ralph.py:377 | `consecutive_empty` counter is reset when content is non-empty, but if the model returns content without tool calls repeatedly (not empty, just no tools), this doesn't catch that failure mode. | Add a separate counter for "no tool calls" regardless of content presence, with a higher threshold. |
| 6 | ralph.py:445 | `TOOL_RESULT_MAX_CHARS = cfg.get("max_tool_output_chars", 32000)` - 32k chars is ~8k tokens, but the truncation message says `~8k tokens` which is inaccurate (32k chars is more like 6-8k tokens depending on content). | Fix the comment to say `~6-8k tokens` or calculate based on actual token estimation. |
| 7 | ralph.py:467 | When `task_complete` is called, the loop breaks immediately without processing remaining tool results in the same response. If the model calls multiple tools including task_complete, other tool results are lost. | Process all tool results before breaking, or log a warning that other tool calls were ignored. |
| 8 | ralph.py:535 | `archive_if_branch_changed(slug, prd)` is called but the function signature in prd_manager expects different parameters - need to verify this matches. | Verify the function signature in prd_manager.py matches this call. |

---

## 🔵 Minor (nice to have)
| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | ralph.py:42 | Duplicate import of `append_progress` on lines 42 and 44 - imported twice from prd_manager. | Remove duplicate import. |
| 2 | ralph.py:102 | Variable `_qc_raw` uses underscore prefix suggesting private variable, but it's just a local temp variable. | Rename to `qc_raw` for consistency with other local variable naming. |
| 3 | ralph.py:153 | `context_files_section = "(none specified - use list_dir to explore)"` - this string is shown to the model but `list_dir` is not always available as a tool (depends on tools.py). | Verify `list_dir` is always available or change the message to be more generic. |
| 4 | ralph.py:201 | `response_id = chunks[0].get("id", "")` - if chunks is empty after filtering, this would fail, but there's a check `if not chunks: raise ValueError` before this. The check is good but the error message could be more specific. | Change error message to include context: `f"No valid SSE chunks found in response from {url}"` |
| 5 | ralph.py:267 | `LOG_INTERVAL = 50` is defined inside the function - should be a module-level constant for consistency. | Move to module-level with other constants like `TOOL_RESULT_MAX_CHARS`. |
| 6 | ralph.py:330 | `max_consecutive_empty = 3` is a magic number - what if the model legitimately needs 4 empty responses? | Add a config option or at least a comment explaining why 3 was chosen. |
| 7 | ralph.py:335 | `repetition_threshold = 6` - another magic number for loop detection. | Add config option or comment explaining the threshold choice. |
| 8 | ralph.py:560 | The `finally` block releases the lock, but if `sys.exit(1)` is called in the `if not acquire_lock` check, the lock was never acquired so release is a no-op. This is fine but could be clearer. | Add a comment explaining that release_lock is safe to call even if lock was never acquired. |

---

## ✅ Verified Working
- SSE streaming and parsing correctly reconstructs tool calls from chunked responses
- Tool call extraction from content handles both <tool_call> JSON</tool_call> and XML formats for model compatibility
- Context size estimation prevents OOM by checking before each model call
- Retry logic with exponential backoff for 429/503/timeout errors
- Progress tracking via progress.txt with configurable max lines
- Lock acquisition prevents concurrent runs on the same project
- Quality check substitution of `{file}` with first contextFile works correctly
- Telegram notification fallback from env var to .env file parsing

---

## Reviewer Notes
- **Systemic risk:** The quality check runner uses `tool_run_command` which doesn't exist - this is a critical bug that will cause ALL quality checks to fail. This needs immediate attention.
- **Test gaps:** No unit tests visible for the core loop logic, tool call parsing, or error handling paths.
- **Design issue:** The loop detection mechanism is simplistic - it only detects exact argument repetition, not semantic loops (e.g., reading the same file 10 times with different line numbers).
- **Observability:** Good logging throughout, but no metrics export (e.g., to Prometheus) for monitoring Ralph's health in production.
- **Configuration:** Many magic numbers (32768 ctx, 160 max tool calls, 3 consecutive empty, etc.) should be configurable via config.yaml.
