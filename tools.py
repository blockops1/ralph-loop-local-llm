"""
tools.py — Ralph Loop tool definitions and executors.

Provides:
  - TOOL_DEFINITIONS: OpenAI-format tool specs to pass in each API request
  - execute_tool(name, args, cwd): dispatches tool calls to actual functions
"""

import os
import subprocess
import json
import logging
from pathlib import Path

log = logging.getLogger("ralph.tools")

WORKSPACE = Path("/Users/yourname/workspace")


# ---------------------------------------------------------------------------
# Simple utility functions
# ---------------------------------------------------------------------------

def hello_world() -> str:
    """Return a greeting string to verify the ralph loop works end-to-end."""
    return "Hello, World!"


def farewell_world() -> str:
    """Call hello_world() and return a farewell string."""
    hello_world()
    return "Goodbye, World!"


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Use this to inspect existing code before making changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file, relative to workspace root or absolute."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating it (and any parent directories) if it does not exist. Overwrites existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file, relative to workspace root or absolute."
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write to the file."
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path, relative to workspace root or absolute."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return stdout, stderr, and exit code. Use for quality checks, tests, or inspecting the environment. Commands run from workspace root unless cwd is specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run."
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional, defaults to workspace root)."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Check git status — shows modified, staged, and untracked files.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes and create a git commit. Only call this after quality checks pass.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Commit message. Should be concise and describe what changed."
                    }
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Signal that the current story is fully implemented and all acceptance criteria are met. Include a brief summary of what was done and any key learnings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What was implemented and any important notes for future iterations."
                    }
                },
                "required": ["summary"]
            }
        }
    }
]


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

def _resolve_path(path: str) -> Path:
    """Resolve path relative to workspace if not absolute."""
    p = Path(path)
    if p.is_absolute():
        return p
    return WORKSPACE / p


def tool_read_file(path: str) -> str:
    resolved = _resolve_path(path)
    log.info(f"read_file: {resolved}")
    if not resolved.exists():
        return f"ERROR: File not found: {resolved}"
    if not resolved.is_file():
        return f"ERROR: Not a file: {resolved}"
    try:
        content = resolved.read_text(encoding="utf-8")
        lines = content.splitlines()
        if len(lines) > 500:
            return "\n".join(lines[:500]) + f"\n... [truncated — {len(lines)} total lines]"
        return content
    except Exception as e:
        return f"ERROR reading file: {e}"


def tool_write_file(path: str, content: str) -> str:
    resolved = _resolve_path(path)
    log.info(f"write_file: {resolved} ({len(content)} chars)")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"OK: Written {len(content)} chars to {resolved}"
    except Exception as e:
        return f"ERROR writing file: {e}"


def tool_list_dir(path: str) -> str:
    resolved = _resolve_path(path)
    log.info(f"list_dir: {resolved}")
    if not resolved.exists():
        return f"ERROR: Path not found: {resolved}"
    if not resolved.is_dir():
        return f"ERROR: Not a directory: {resolved}"
    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for entry in entries:
            marker = "/" if entry.is_dir() else ""
            lines.append(f"{entry.name}{marker}")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"ERROR listing directory: {e}"


def tool_run_command(command: str, cwd: str = None, timeout: int = 60) -> str:
    work_dir = _resolve_path(cwd) if cwd else WORKSPACE
    log.info(f"run_command: {command!r} (cwd={work_dir})")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
            timeout=timeout,
        )
        output_parts = []
        if result.stdout.strip():
            output_parts.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            output_parts.append(f"STDERR:\n{result.stderr.strip()}")
        output_parts.append(f"EXIT CODE: {result.returncode}")
        return "\n".join(output_parts) if output_parts else f"EXIT CODE: {result.returncode}"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR running command: {e}"


def tool_git_status() -> str:
    log.info("git_status")
    return tool_run_command("git status --short && git log --oneline -5", cwd=str(WORKSPACE))


def tool_git_commit(message: str) -> str:
    log.info(f"git_commit: {message!r}")
    # Check if there are any changes to commit
    status_result = tool_run_command('git status --porcelain', cwd=str(WORKSPACE))
    if not status_result.strip():
        # Nothing to commit — already committed or no changes
        return "OK: Nothing to commit (working tree clean)"
    for _git_attempt in range(3):
        result = tool_run_command(f'git add -A && git commit -m {json.dumps(message)}', cwd=str(WORKSPACE))
        if 'EXIT CODE: 0' in result:
            return result
        log.warning(f'Git commit attempt {_git_attempt+1}/3 failed, retrying in 3s...')
        import time as _time; _time.sleep(3)
    return result  # return final result after 3 attempts


def tool_task_complete(summary: str) -> str:
    """Signals completion — handled by ralph.py, not actually executed here."""
    log.info(f"task_complete: {summary[:100]}")
    return f"TASK_COMPLETE: {summary}"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

EXECUTORS = {
    "read_file": lambda args: tool_read_file(args["path"]),
    "write_file": lambda args: tool_write_file(args["path"], args["content"]),
    "list_dir": lambda args: tool_list_dir(args["path"]),
    "run_command": lambda args: tool_run_command(args["command"], args.get("cwd")),
    "git_status": lambda args: tool_git_status(),
    "git_commit": lambda args: tool_git_commit(args["message"]),
    "task_complete": lambda args: tool_task_complete(args["summary"]),
}


def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name with the given arguments. Returns string result."""
    if name not in EXECUTORS:
        return f"ERROR: Unknown tool '{name}'. Available: {list(EXECUTORS.keys())}"
    try:
        return EXECUTORS[name](args)
    except KeyError as e:
        return f"ERROR: Missing required argument {e} for tool '{name}'"
    except Exception as e:
        log.exception(f"Tool '{name}' raised exception")
        return f"ERROR in tool '{name}': {e}"
