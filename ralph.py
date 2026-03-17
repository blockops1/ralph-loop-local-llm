#!/usr/bin/env python3
"""
ralph.py - Ralph Loop main orchestrator.

Autonomous coding loop for the local LLM. Reads prd.json, picks the next
incomplete story, runs an agentic loop (multi-turn tool execution) with the
local model, quality-checks the result, commits to git, marks the story done,
and repeats until all stories pass or limits are hit.

Usage:
    python3 ralph.py <project-slug> [--story STORY_ID] [--dry-run]
    python3 ralph.py --list-projects

Environment:
    Reads config from ralph/config.yaml
    Projects: ralph/projects/<slug>/prd.json
"""

import argparse
import json
import logging
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

import yaml
import requests

# Sibling imports
RALPH_DIR = Path(__file__).parent
sys.path.insert(0, str(RALPH_DIR))
from tools import TOOL_DEFINITIONS, execute_tool, WORKSPACE
from prd_manager import (
    load_prd, save_prd, get_next_story, all_done, any_blocked,
    mark_story_done, mark_story_failed, mark_story_blocked, get_blocked_stories,
    append_progress, story_summary,
    append_progress, get_progress_context, init_progress,
    archive_if_branch_changed, acquire_lock, release_lock,
    agents_md_path, project_dir, list_active_projects,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(slug: str, cfg: dict) -> logging.Logger:
    log_dir = RALPH_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    # Normalize slug: strip any path prefix (e.g., "projects/base-trader" -> "base-trader")
    clean_slug = Path(slug).name
    log_file = log_dir / f"{clean_slug}-{date_str}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("ralph")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg_path = RALPH_DIR / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_system_prompt(story: dict, progress: str, agents_md: str) -> str:
    """Fill PROMPT.md template with story, progress, and AGENTS.md content."""
    template_path = RALPH_DIR / "PROMPT.md"
    template = template_path.read_text(encoding="utf-8")

    # Build story block
    criteria = "\n".join(f"  - {c}" for c in story.get("acceptanceCriteria", []))
    _qc_raw = story.get("qualityChecks", [])
    _qc_list = [_qc_raw] if isinstance(_qc_raw, str) and _qc_raw.strip() else (_qc_raw if isinstance(_qc_raw, list) else [])
    quality_checks = "\n".join(f"  - {q}" for q in _qc_list)

    # Pre-load contextFiles contents into the prompt so the model never needs to read them
    context_file_list = story.get("contextFiles", [])
    context_files_section = ""
    if context_file_list:
        parts = []
        for cf in context_file_list:
            cf_path = WORKSPACE / cf
            if cf_path.exists():
                content = cf_path.read_text(encoding="utf-8", errors="replace")
                parts.append(f"### {cf}\n```\n{content}\n```")
            else:
                parts.append(f"### {cf}\n(file does not exist yet - create it)")
        context_files_section = "\n\n".join(parts)
    else:
        context_files_section = "(none specified - use list_dir to explore)"

    story_block = f"""**Story ID:** {story['id']}
**Title:** {story['title']}
**Description:** {story.get('description', '')}

**Acceptance Criteria:**
{criteria}

**Context Files (already loaded below - do NOT call read_file on these):**
{chr(10).join(f"  - {f}" for f in context_file_list) if context_file_list else "  (none)"}

**Quality Checks (run before committing):**
{quality_checks if quality_checks else '  (none specified)'}

**Previous error (if retry):** {story.get('error', '') or 'none'}

## Pre-loaded File Contents

These files are already in your context. Do NOT read them again with read_file.

{context_files_section}"""

    template = template.replace("{{STORY_BLOCK}}", story_block)
    template = template.replace("# Ralph Progress Log - ralph-blocked-status\nStarted: 2026-03-17T18:43:53.562828\n---", progress or "(no prior progress)")
    template = template.replace("(no AGENTS.md found)", agents_md or "(no AGENTS.md found)")

    return template


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def estimate_messages_tokens(messages: list) -> int:
    """Estimate total tokens across all messages."""
    return sum(estimate_tokens(str(m.get("content", "") or "")) for m in messages)


# ---------------------------------------------------------------------------
# Local model API call
# ---------------------------------------------------------------------------

def parse_sse_to_completion(sse_text: str) -> dict:
    """
    Parse an SSE event stream into a standard chat.completion dict.
    Handles streamed tool_calls and content from the proxy.
    """
    chunks = []
    for line in sse_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunks.append(json.loads(data))
        except json.JSONDecodeError:
            continue

    if not chunks:
        raise ValueError("No valid SSE chunks found in response")

    # Reconstruct finish_reason, content, tool_calls from deltas
    content_parts = []
    tool_calls_map: dict[int, dict] = {}  # index -> tool_call dict
    finish_reason = "stop"
    response_id = chunks[0].get("id", "")
    usage = None

    for chunk in chunks:
        # Usage chunk (no choices)
        if "usage" in chunk and not chunk.get("choices"):
            usage = chunk["usage"]
            continue

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

            # Accumulate content
            if delta.get("content"):
                content_parts.append(delta["content"])

            # Accumulate tool_calls
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.get("id"):
                    tool_calls_map[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_calls_map[idx]["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    tool_calls_map[idx]["function"]["arguments"] += fn["arguments"]

    message: dict = {"role": "assistant"}
    if content_parts:
        message["content"] = "".join(content_parts)
    else:
        message["content"] = ""
    if tool_calls_map:
        message["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map)]

    result = {
        "id": response_id,
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        result["usage"] = usage
    return result


def extract_tool_calls_from_content(content: str) -> list:
    """
    Extract tool calls from content text. Handles two formats:

    Format 1 (preferred): <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    Format 2 (fallback):  <tool_calls><write_file><parameter=path>...</parameter>...
                          (Qwen-style XML parameter format)

    Returns a list of OpenAI-format tool_call dicts, or [] if none found.
    """
    import re
    tool_calls = []

    # Format 1: <tool_call>JSON</tool_call>
    for i, m in enumerate(re.finditer(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
            name = data.get("name", "")
            arguments = data.get("arguments", data.get("parameters", {}))
            tool_calls.append({
                "id": f"extracted_{i}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments) if not isinstance(arguments, str) else arguments,
                },
            })
        except (json.JSONDecodeError, AttributeError):
            continue

    if tool_calls:
        return tool_calls

    # Format 2: <tool_calls><tool_name><parameter=key>value</parameter>...</tool_name></tool_calls>
    # Qwen/Claude-style XML - model outputs this when it ignores the JSON format instruction
    known_tools = {"read_file", "write_file", "list_dir", "run_command",
                   "git_status", "git_commit", "task_complete"}
    outer = re.search(r"<tool_calls>(.*?)</tool_calls>", content, re.DOTALL)
    if outer:
        block = outer.group(1)
        for tool_name in known_tools:
            tm = re.search(rf"<{tool_name}>(.*?)</{tool_name}>", block, re.DOTALL)
            if not tm:
                continue
            inner = tm.group(1)
            # Extract <parameter=key>value</parameter> pairs
            arguments = {}
            for pm in re.finditer(r"<parameter=(\w+)>(.*?)</parameter>", inner, re.DOTALL):
                arguments[pm.group(1)] = pm.group(2).strip()
            # Also try plain <key>value</key> pairs (alternate XML style)
            if not arguments:
                for pm in re.finditer(r"<(\w+)>(.*?)</\1>", inner, re.DOTALL):
                    arguments[pm.group(1)] = pm.group(2).strip()
            tool_calls.append({
                "id": f"xml_{tool_name}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments),
                },
            })

    return tool_calls



def estimate_num_ctx(messages: list) -> int:
    """Return num_ctx for Ollama. Start at 32768 and scale up if content is larger."""
    total_chars = sum(len(str(m.get("content") or "")) for m in messages)
    tokens = int(total_chars / 3.0) + 4096
    ctx = 32768
    while ctx < tokens and ctx < 131072:
        ctx *= 2
    return ctx


def call_model_with_heartbeat(
    messages: list,
    cfg: dict,
    log: logging.Logger,
    label: str = "",
    with_tools: bool = True,
) -> dict:
    """Passthrough - streaming now provides live visibility; no separate heartbeat needed."""
    return call_model(messages, cfg, log=log, label=label, with_tools=with_tools)


def call_model(
    messages: list,
    cfg: dict,
    log: logging.Logger = None,
    label: str = "",
    with_tools: bool = True,
) -> dict:
    """
    Call the local model API with streaming enabled.
    Logs a progress line every LOG_INTERVAL tokens so the log is never silent.
    Returns a standard chat.completion dict assembled from SSE chunks.
    """
    LOG_INTERVAL = 50  # log a line every N tokens received

    url = cfg["model_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model_id"],
        "messages": messages,
        "max_tokens": cfg.get("max_tokens", 16384),
        "temperature": 0.2,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if with_tools:
        payload["tools"] = TOOL_DEFINITIONS
        payload["tool_choice"] = "auto"

    resp = requests.post(
        url,
        json=payload,
        timeout=cfg.get("request_timeout", 3600),
        headers={"Content-Type": "application/json"},
        stream=True,
    )
    if not resp.ok:
        body = resp.text[:500] if resp.text else "(empty)"
        raise requests.HTTPError(
            f"{resp.status_code} Client Error: {resp.reason} for url: {url} - body: {body}",
            response=resp,
        )

    # Parse SSE stream chunk-by-chunk with live logging
    content_parts = []
    tool_calls_map: dict[int, dict] = {}
    finish_reason = "stop"
    response_id = ""
    usage = None
    token_count = 0
    last_logged = 0
    start_time = time.time()

    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        if not response_id:
            response_id = chunk.get("id", "")

        # Usage-only chunk
        if "usage" in chunk and not chunk.get("choices"):
            usage = chunk["usage"]
            continue

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

            if delta.get("content"):
                tok = delta["content"]
                content_parts.append(tok)
                token_count += 1
                if log and (token_count - last_logged) >= LOG_INTERVAL:
                    elapsed = time.time() - start_time
                    preview = "".join(content_parts)[-80:].replace("\n", " ")
                    log.info(f"_ Streaming [{label}] {token_count} tokens | {elapsed:.0f}s | ...{preview}")
                    last_logged = token_count

            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.get("id"):
                    tool_calls_map[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_calls_map[idx]["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    tool_calls_map[idx]["function"]["arguments"] += fn["arguments"]

    elapsed_total = time.time() - start_time
    if log:
        tool_names = [v["function"]["name"] for v in tool_calls_map.values()] if tool_calls_map else []
        log.info(f"_ Stream complete [{label}] | {token_count} tokens | {elapsed_total:.1f}s | finish={finish_reason} | tools={tool_names or 'none'}")

    message: dict = {"role": "assistant"}
    message["content"] = "".join(content_parts) if content_parts else ""
    if tool_calls_map:
        message["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map)]

    result = {
        "id": response_id,
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        result["usage"] = usage
    return result


# ---------------------------------------------------------------------------
# Agentic loop (single story)
# ---------------------------------------------------------------------------

def run_story_loop(story: dict, cfg: dict, log: logging.Logger, dry_run: bool = False) -> tuple[bool, str]:
    """
    Run the agentic loop for a single story.
    Returns (success: bool, summary: str).
    """
    slug = story.get("_slug", "unknown")

    # Build context
    progress = get_progress_context(slug, max_lines=cfg.get("max_progress_lines", 200))
    agents_md = ""
    agents_path = agents_md_path(slug)
    if agents_path.exists():
        agents_md = agents_path.read_text(encoding="utf-8")

    system_prompt = build_system_prompt(story, progress, agents_md)

    token_estimate = estimate_tokens(system_prompt)
    log.info(f"System prompt: ~{token_estimate} tokens")

    if token_estimate > cfg.get("max_context_tokens", 262000):
        msg = f"Context too large ({token_estimate} estimated tokens > {cfg['max_context_tokens']} limit). Reduce contextFiles or progress.txt."
        log.error(msg)
        return False, msg

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Please implement story {story['id']}: {story['title']}"},
    ]

    max_tool_calls = cfg.get("max_tool_calls_per_story", 160)
    tool_call_count = 0
    completion_summary = None
    consecutive_empty = 0  # consecutive empty-content no-tool-call responses
    max_consecutive_empty = 3  # fail fast if model stops responding

    # Loop detection: track recent tool call signatures
    recent_calls = []  # list of (name, args_key) tuples
    max_recent_calls = 20  # window for repetition detection
    repetition_threshold = 6  # fail if same call pattern seen this many times

    log.info(f"Starting agentic loop for {story['id']}: {story['title']}")
    story_start_time = time.time()

    while tool_call_count < max_tool_calls:
        elapsed = time.time() - story_start_time
        log.info(f"Model call #{tool_call_count + 1} (messages={len(messages)}, elapsed={elapsed:.0f}s)")

        if dry_run:
            log.info("[DRY RUN] Would call model here. Stopping.")
            return False, "DRY_RUN"

        # Retry up to 3 times on transient errors (429 concurrency, 503, timeout)
        response = None
        call_start = time.time()
        call_label = f"{story['id']} call #{tool_call_count + 1}"
        for _attempt in range(3):
            try:
                response = call_model_with_heartbeat(messages, cfg, log, label=call_label, with_tools=True)
                break
            except requests.Timeout:
                log.warning(f"Model API timeout (attempt {_attempt+1}/3) - retrying in 30s")
                time.sleep(30)
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "503" in err_str or "502" in err_str:
                    wait = 30 * (_attempt + 1)
                    log.warning(f"Model API {err_str[:60]} (attempt {_attempt+1}/3) - retrying in {wait}s")
                    time.sleep(wait)
                else:
                    log.error(f"Model API error: {e}")
                    return False, f"Model API error: {e}"
        if response is None:
            log.error("Model API failed after 3 retries")
            return False, "Model API failed after 3 retries (429/timeout)"

        call_elapsed = time.time() - call_start
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", "?")
        completion_tokens = usage.get("completion_tokens", "?")
        log.info(f"Model response in {call_elapsed:.1f}s | tokens: prompt={prompt_tokens} completion={completion_tokens} | finish={finish_reason}")

        # Append assistant message to history
        messages.append({"role": "assistant", **{k: v for k, v in message.items() if k != "role"}})

        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""

        # Fallback: extract tool calls from content if no structured tool_calls
        # Handles both <tool_call>JSON</tool_call> and Qwen XML parameter format
        if not tool_calls and ("<tool_call>" in content or "<tool_calls>" in content):
            extracted = extract_tool_calls_from_content(content)
            if extracted:
                import re as _re
                log.info(f"Extracted {len(extracted)} tool call(s) from content text")
                clean_content = _re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=_re.DOTALL).strip()
                tool_calls = extracted
                # Fix last assistant message in history to have clean content + tool_calls
                messages[-1] = {
                    "role": "assistant",
                    "content": clean_content or "",
                    "tool_calls": tool_calls,
                }

        # Guard: fail fast on model error responses (502, backend errors, etc.)
        # Prevents infinite loops when model server returns errors instead of doing work.
        # Without this, Ralph appends the error to history and retries, growing context
        # until it hits the 250K token limit (observed: 152 loops before 413 error).
        if content and ("[backend error" in content or "[error" in content or content.startswith("[5")):
            log.error(f"Model returned error content: {content[:200]}")
            return False, f"Model server error: {content[:200]}"

        if not tool_calls:
            # No tool call - model may be done or stuck
            if "TASK_COMPLETE" in content or "<promise>COMPLETE</promise>" in content:
                completion_summary = content
                log.info("Completion signal in content")
                break
            log.warning(f"No tool calls, finish_reason={finish_reason}. Content: {content[:200]}")
            if not content:
                consecutive_empty += 1
                log.warning(f"Empty response #{consecutive_empty}/{max_consecutive_empty}")
                if consecutive_empty >= max_consecutive_empty:
                    return False, f"Model returned {consecutive_empty} consecutive empty responses - model stuck or refusing tools"
            else:
                consecutive_empty = 0
            if finish_reason in ("stop", "length"):
                # Nudge the model
                messages.append({
                    "role": "user",
                    "content": "Call a tool now. Do not output text - make a tool call immediately."
                })
                tool_call_count += 1
                continue
            break

        # Guard: check total message context before each model call
        ctx_tokens = estimate_messages_tokens(messages)
        max_ctx = cfg.get("max_context_tokens", 262000)
        if ctx_tokens > max_ctx:
            msg = f"Message history too large ({ctx_tokens} est tokens > {max_ctx} limit) - stopping to avoid OOM."
            log.error(msg)
            return False, msg
        log.info(f"Context size: ~{ctx_tokens} tokens")

        # Execute each tool call
        TOOL_RESULT_MAX_CHARS = cfg.get("max_tool_output_chars", 32000)  # ~8k tokens, fits in 262K context
        tool_results = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            # Log tool name + first 120 chars of args for visibility
            args_preview = json.dumps(args)[:120].replace("\n", " ")
            log.info(f"Tool call: {name} | args: {args_preview}")

            # Loop detection: track this call signature
            # Create a simple key from tool name + sorted arg keys + first 50 chars of arg values
            arg_key = json.dumps(args, sort_keys=True)[:100]
            call_signature = (name, arg_key)
            recent_calls.append(call_signature)
            if len(recent_calls) > max_recent_calls:
                recent_calls.pop(0)

            # Check for repetition
            repetition_count = recent_calls.count(call_signature)
            if repetition_count >= repetition_threshold:
                msg = f"LOOP DETECTED: Tool '{name}' with same arguments called {repetition_count} times in last {len(recent_calls)} calls. Stopping."
                log.error(msg)
                return False, msg

            result = execute_tool(name, args)
            result_str = str(result)
            if len(result_str) > TOOL_RESULT_MAX_CHARS:
                result_str = result_str[:TOOL_RESULT_MAX_CHARS] + f"\n[... truncated - {len(result_str)} chars total]"
                log.info(f"Tool result ({name}): truncated to {TOOL_RESULT_MAX_CHARS} chars")
            else:
                log.info(f"Tool result ({name}): {result_str[:200]}")

            tool_results.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{tool_call_count}"),
                "content": result_str,
            })

            # Check for task_complete signal
            if name == "task_complete":
                completion_summary = args.get("summary", "")
                log.info(f"task_complete called: {completion_summary[:100]}")

        messages.extend(tool_results)
        tool_call_count += len(tool_calls)

        if completion_summary is not None:
            break

    # Determine outcome
    if completion_summary is not None:
        failed = completion_summary.startswith("FAILED:")
        if failed:
            log.warning(f"Story self-reported failure: {completion_summary}")
            return False, completion_summary
        log.info(f"Story complete: {completion_summary[:100]}")
        return True, completion_summary

    log.warning(f"Story loop ended without task_complete (tool_calls={tool_call_count})")
    return False, f"Loop ended without completion signal after {tool_call_count} tool calls"


# ---------------------------------------------------------------------------
# Quality check runner
# ---------------------------------------------------------------------------

def run_quality_checks(story: dict, log: logging.Logger) -> tuple[bool, str]:
    """Run quality checks from prd.json. Returns (passed, output)."""
    checks = story.get("qualityChecks", [])
    # Normalize: qualityChecks may be a string (single command) or a list
    if isinstance(checks, str):
        checks = [checks] if checks.strip() else []
    if not checks:
        log.info("No quality checks defined")
        return True, "No quality checks"

    from tools import tool_run_command, WORKSPACE
    outputs = []
    for check in checks:
        # Replace {file} with first contextFile if present
        context_files = story.get("contextFiles", [])
        if context_files:
            check = check.replace("{file}", context_files[0])
        log.info(f"Quality check: {check}")
        result = tool_run_command(check, timeout=60)
        outputs.append(f"$ {check}\n{result}")
        if "EXIT CODE: 0" not in result:
            log.warning(f"Quality check FAILED: {check}")
            return False, "\n\n".join(outputs)

    return True, "\n\n".join(outputs)


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def notify(msg: str, log: logging.Logger) -> None:
    """Send a Telegram message directly to Mr. V via bot API."""
    import os, re
    # Prefer env var (set by ralph.sh sourcing .env), fall back to file parse
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        env_path = Path.home() / ".openclaw" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                m = re.match(r'(?:export\s+)?TELEGRAM_BOT_TOKEN=["\']?([^"\'\\s]+)["\']?', line)
                if m:
                    token = m.group(1)
                    break
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not found - cannot notify")
        return
    chat_id = "374999219"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"_ Ralph: {msg}"},
            timeout=10,
        )
        if resp.ok:
            log.info(f"Telegram notification sent: {msg[:60]}")
        else:
            log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ralph Loop - autonomous coding agent")
    parser.add_argument("slug", nargs="?", help="Project slug (matches ralph/projects/<slug>/)")
    parser.add_argument("--story", help="Force a specific story ID")
    parser.add_argument("--dry-run", action="store_true", help="Plan only - don't call model or commit")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--list-projects", action="store_true", help="List all active project slugs and exit")
    parser.add_argument("--version", action="store_true", help="Print ralph version and exit")
    args = parser.parse_args()

    # Handle --version flag
    if args.version:
        version_file = RALPH_DIR / "VERSION"
        print(version_file.read_text().strip())
        sys.exit(0)

    # Handle --list-projects flag
    if args.list_projects:
        projects = list_active_projects()
        for slug in projects:
            print(slug)
        sys.exit(0)

    # Require slug for normal operation
    if not args.slug:
        parser.error("Project slug is required (or use --list-projects)")

    # Normalize slug: strip any path prefix (e.g., "projects/base-trader" -> "base-trader")
    args.slug = Path(args.slug).name

    cfg = load_config()
    if args.max_iterations:
        cfg["max_iterations"] = args.max_iterations

    log = setup_logging(args.slug, cfg)
    log.info(f"=== Ralph Loop starting: project={args.slug} dry_run={args.dry_run} ===")

    # Lock
    if not args.dry_run:
        if not acquire_lock(args.slug):
            log.error(f"Project '{args.slug}' is locked - another ralph process may be running.")
            sys.exit(1)

    try:
        _run(args, cfg, log)
    finally:
        if not args.dry_run:
            release_lock(args.slug)


def _run(args, cfg: dict, log: logging.Logger):
    slug = args.slug

    from loop_runner import run_all_stories
    # Load PRD
    try:
        prd = load_prd(slug)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    # Initialize progress if needed
    init_progress(slug)

    # Archive if branch changed
    archive_if_branch_changed(slug, prd)

    prd = run_all_stories(args, cfg, log, prd, slug)

    if all_done(prd):
        log.info("All stories complete!")
        notify(f"All stories complete in '{slug}'", log)
    elif any_blocked(prd, cfg.get("max_attempts_per_story", 3)):
        max_att = cfg.get("max_attempts_per_story", 3)
        blocked = get_blocked_stories(prd, max_att)
        for b_story in blocked:
            mark_story_blocked(prd, b_story["id"], b_story.get("error", "max attempts reached"))
        save_prd(slug, prd)
        # Build rich notification with story ID + last error for each blocked story
        lines = [f"_ Ralph blocked in '{slug}' - manual intervention needed"]
        for b_story in blocked:
            sid = b_story["id"]
            reason = b_story.get("error", "unknown")[:120]
            lines.append(f"  * {sid}: {reason}")
        notify("\n".join(lines), log)
        log.warning("\n".join(lines))



if __name__ == "__main__":
    main()
