# Code Review: ralph.py Line-Level Bug Audit

**Date:** 2026-03-28T12:00:00Z
**Reviewer:** Ralph Review Agent
**File reviewed:** ralph.py (848 lines)
**Story:** REV-A

---

## Summary
ralph.py is the main orchestrator for the Ralph autonomous coding loop. It reads PRDs, runs agentic loops with a local LLM, executes tools, quality-checks results, and commits to git. The codebase is generally well-structured but contains several line-level bugs including missing None checks, potential crashes on malformed data, and logic issues that could cause failures in production.

---

## 🔴 Critical (must fix before ship)

| # | File | Issue | Impact | Fix |
|---|------|-------|--------|-----|
| 1 | ralph.py:138 | `story['id']` accessed directly without `.get()`; if PRD story lacks "id" key, crashes with KeyError | Crashes when processing malformed PRD | Use `story.get('id', 'unknown')` |
| 2 | ralph.py:138 | `story['title']` accessed directly without `.get()`; if PRD story lacks "title" key, crashes with KeyError | Crashes when processing malformed PRD | Use `story.get('title', 'Untitled')` |
| 3 | ralph.py:499 | `story['id']` and `story['title']` accessed directly in user message; same KeyError risk | Crashes on first model call if story malformed | Use `.get()` with defaults |
| 4 | ralph.py:513 | `story['id']` and `story['title']` accessed directly in log message; same KeyError risk | Crashes at loop start if story malformed | Use `.get()` with defaults |
| 5 | ralph.py:527 | `story['id']` accessed directly in call_label; same KeyError risk | Crashes on model call if story malformed | Use `story.get('id', 'unknown')` |
| 6 | ralph.py:834 | `b_story["id"]` accessed directly; if `get_blocked_stories` returns story without "id", crashes | Crashes when building blocked story notification | Use `b_story.get("id", "unknown")` |
| 7 | ralph.py:839 | `b_story["id"]` accessed directly in notification loop; same KeyError risk | Crashes when building Telegram notification | Use `b_story.get("id", "unknown")` |
| 8 | ralph.py:232 | `tc["id"]` accessed directly after `tc.get("id")` check; if "id" missing, crashes | Crashes on malformed SSE tool_call chunk | Use `tc.get("id", "")` |
| 9 | ralph.py:442 | `tc["id"]` accessed directly in `call_model()`; same issue as line 232 | Crashes on malformed SSE tool_call chunk | Use `tc.get("id", "")` |
| 10 | ralph.py:677 | `completion_summary.startswith("FAILED:")` assumes string; if completion_summary is non-string non-None, crashes | Crashes with AttributeError if model returns non-string summary | Check `isinstance(completion_summary, str)` before `.startswith()` |

---

## ⚠️ Medium (fix before next sprint)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | ralph.py:71 | Log file has no rotation; single file grows indefinitely | Implement `RotatingFileHandler` to prevent disk exhaustion |
| 2 | ralph.py:115 | `cf_path.read_text(encoding="utf-8", errors="replace")` silently replaces invalid UTF-8 | Log warning when replacement occurs or use `errors="strict"` |
| 3 | ralph.py:235-237 | Uses `fn.get("name")` in condition but `fn["name"]` in body; inconsistent pattern | Use `fn.get("name", "")` consistently throughout |
| 4 | ralph.py:444-447 | Same inconsistent pattern as line 235-237 in `call_model()` | Use `fn.get("name", "")` consistently |
| 5 | ralph.py:251 | `chat_template_kwargs={"enable_thinking": False}` hardcoded; may not work with all backends | Make configurable via config.yaml |
| 6 | ralph.py:271 | `timeout=cfg.get("request_timeout", 3600)` - 1 hour default is excessive | Reduce default to 300s (5 min) |
| 7 | ralph.py:380 | `max_consecutive_empty = 3` hardcoded magic number | Move to config.yaml as `max_consecutive_empty_responses` |
| 8 | ralph.py:385 | `repetition_threshold = 6` hardcoded magic number | Move to config.yaml as `loop_detection_threshold` |
| 9 | ralph.py:410 | `time.sleep(30)` blocking sleep wastes resources | Use async/await with timeout or signal-based cancellation |
| 10 | ralph.py:453 | `content.startswith("[5")` fragile error detection; only catches specific format | Use regex `r"^\[\d{3}"` to catch all HTTP error codes |
| 11 | ralph.py:489 | `TOOL_RESULT_MAX_CHARS` truncation may cut mid-JSON, breaking downstream parsing | Truncate at safe boundary or wrap in JSON array |
| 12 | ralph.py:543 | Quality check `timeout=60` hardcoded; some checks may need longer | Make configurable via story-level `qualityCheckTimeout` field |
| 13 | ralph.py:567 | `chat_id = "374999219"` hardcoded Telegram chat ID | Move to config.yaml or environment variable |
| 14 | ralph.py:615 | `args.slug = Path(args.slug).name` silently strips path prefix | Log warning when slug is normalized from path |
| 15 | ralph.py:641 | Telegram notification may exceed 4096 char limit | Truncate or paginate long notifications |

---

## 🔵 Minor (nice to have)

| # | File | Issue | Suggestion |
|---|------|-------|------------|
| 1 | ralph.py:38 | `import yaml` at module level but only used in `load_config()` | Move import inside function for faster startup |
| 2 | ralph.py:40 | `import requests` at module level but only used in specific functions | Consider lazy import for faster startup |
| 3 | ralph.py:91 | `story_type = story.get("type", "create")` pattern repeated | Extract to helper function `get_story_type(story)` |
| 4 | ralph.py:133 | `story_block` f-string spans many lines; hard to read | Use separate variables for each section |
| 5 | ralph.py:151 | `estimate_tokens()` uses rough `len(text) // 4` estimate | Use more accurate token estimation if available |
| 6 | ralph.py:301 | `LOG_INTERVAL = 50` hardcoded logging frequency | Move to config.yaml |
| 7 | ralph.py:358 | Leading underscore in log message `"_ Stream complete..."` is inconsistent | Standardize log message format |
| 8 | ralph.py:425 | `prompt_tokens = usage.get("prompt_tokens", "?")` uses "?" as default | Use 0 or -1 for numeric fields to aid log parsing |
| 9 | ralph.py:501 | Tool result log truncates at 200 chars but earlier truncation is at 32000 | Be consistent with truncation lengths |
| 10 | ralph.py:527 | `log.warning()` for failed story should be `log.error()` | Change to `log.error()` for failed stories |
| 11 | ralph.py:643 | Same lines logged to Telegram and console; potential duplication | Consider different log levels or deduplication |
| 12 | ralph.py:240 | `estimate_num_ctx()` has magic numbers 32768, 131072, 3.0 | Add comments explaining these values |
| 13 | ralph.py:445 | Regex `r"<tool_call>.*?</tool_call>"` may not match all delimiter variations | Use more robust pattern or validate extraction |
| 14 | ralph.py:531 | `tool_run_command` imported inside function; inconsistent | Move import to module level |
| 15 | ralph.py:572 | Telegram message prefix `"_ Ralph: {msg}"` hardcoded | Make prefix configurable |

---

## ✅ Verified Working

- Story loading and PRD parsing with proper error handling
- Tool call extraction handles both JSON (`<tool_call>...</tool_call>`) and XML (`<tool_calls>...`) formats
- SSE streaming with chunk reassembly for tool_calls and content
- Loop detection mechanism prevents infinite loops from repetitive tool calls
- Quality check execution with proper exit code validation
- Git integration via `git_commit` tool (delegated to tools.py)
- Progress tracking and context management across story iterations
- Lock acquisition/release for preventing concurrent runs
- Branch change detection and archival
- Configuration loading from config.yaml
- Logging to both file and stdout with proper formatting
- Dry-run mode for testing without side effects
- Retry logic for transient model API errors (429, 503, 502, timeout)
- Context size monitoring to prevent OOM errors
- Tool output truncation to prevent context overflow
- `consecutive_empty` counter correctly resets on non-empty content
- `messages[-1]` access is safe because assistant message is appended before fallback extraction

---

## Reviewer Notes

1. **Systemic Risk - Direct Dict Access**: Multiple locations use `story['id']` and `story['title']` directly instead of `.get()`. If a PRD is malformed (missing required keys), Ralph will crash with KeyError instead of handling gracefully.

2. **Test Gap**: No unit tests exist for critical parsing functions (`parse_sse_to_completion`, `extract_tool_calls_from_content`, `call_model`). These should be tested with edge cases including malformed input.

3. **Configuration Drift**: Many magic numbers (timeouts, thresholds, limits) are hardcoded instead of configurable. This makes tuning difficult without code changes.

4. **Error Message Quality**: Many error messages are generic ("Model API error: {e}"). Adding more context (URL, request ID, partial response) would help debugging.

5. **Memory Growth Risk**: The `messages` list grows unbounded within a story loop. While context size is checked, there's no mechanism to prune old messages if a story runs for many iterations.

6. **Race Condition Risk**: The lock mechanism (`acquire_lock`, `release_lock`) is delegated to `prd_manager.py`. If that implementation has bugs, multiple Ralph instances could corrupt the same PRD.

7. **Telegram Dependency**: The notification system assumes Telegram is always available. If the bot token is invalid or Telegram is down, notifications fail silently (logged as warning but not surfaced to user).

8. **Quality Check Timing**: Quality checks run AFTER the story completes but BEFORE the git commit. If quality checks fail, the story is marked failed but the code changes remain in the working directory, potentially causing confusion.

9. **SSE Parsing Safety**: The SSE parsing code uses `fn.get("name")` in conditions but `fn["name"]` in assignments. While the condition prevents the crash, this inconsistent pattern is a code smell and could lead to bugs if refactored.

---

## Bug Count Summary

- **Critical:** 10 bugs (will crash on malformed input)
- **Medium:** 15 issues (will cause problems under specific conditions)
- **Minor:** 15 issues (code quality, maintainability)

**Total findings:** 40

**Recommendation:** 🔴 **Do not ship** - Fix critical bugs first, especially the direct dict access issues that will crash on malformed PRDs.
