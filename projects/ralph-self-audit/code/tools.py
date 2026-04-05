"""
tools.py - Ralph Loop tool definitions and executors.

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

WORKSPACE = Path(__file__).resolve().parent.parent


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
            "description": "Check git status - shows modified, staged, and untracked files.",
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
            "description": "Stage all changes and create a LOCAL git commit. Workspace git is a mirror only - NO push/pull. Only call this after quality checks pass.",
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
        if len(lines) > 2000:
            return "\n".join(lines[:2000]) + f"\n... [truncated - {len(lines)} total lines]"
        return content
    except Exception as e:
        return f"ERROR reading file: {e}"


_UNICODE_SUBS = {
    '\u2014': '-',   # em-dash
    '\u2013': '-',   # en-dash
    '\u2018': "'",   # left single quote
    '\u2019': "'",   # right single quote
    '\u201c': '"',   # left double quote
    '\u201d': '"',   # right double quote
    '\u2192': '->',  # right arrow
    '\u2022': '*',   # bullet
    '\u00b7': '.',   # middle dot
    '\u2026': '...', # ellipsis
    '\u00a0': ' ',   # non-breaking space
}

def _sanitize_for_python(content: str, path: str) -> tuple[str, int]:
    """Strip LLM-generated Unicode from .py files. Returns (sanitized, count_replaced)."""
    if not path.endswith('.py'):
        return content, 0
    result = content
    count = 0
    for bad, good in _UNICODE_SUBS.items():
        n = result.count(bad)
        if n:
            result = result.replace(bad, good)
            count += n
    # Catch any remaining non-ASCII chars not in our table
    clean = []
    for c in result:
        if ord(c) > 127:
            clean.append('_')
            count += 1
        else:
            clean.append(c)
    return ''.join(clean), count


def tool_write_file(path: str, content: str) -> str:
    # Guard: reject content that contains truncation artifacts from tool_read_file
    _TRUNCATION_MARKERS = [
        "... [truncated",
        "[truncated -",
        "[truncated -",
        "# ... truncated",
        "# [truncated",
    ]
    for _marker in _TRUNCATION_MARKERS:
        if _marker in content:
            log.error(f"write_file BLOCKED: truncation artifact detected in content for {path}")
            return (
                f"ERROR: Content contains a truncation artifact ({_marker!r}). "
                f"Do NOT write truncated content. "
                f"The file has more lines than shown. "
                f"Re-read the file, then write the COMPLETE content including all lines after the truncation point."
            )
    resolved = _resolve_path(path)
    sanitized, replaced = _sanitize_for_python(content, str(resolved))
    if replaced:
        log.warning(f"write_file: sanitized {replaced} non-ASCII chars from {resolved.name}")
    log.info(f"write_file: {resolved} ({len(sanitized)} chars)")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(sanitized, encoding="utf-8")
        msg = f"OK: Written {len(sanitized)} chars to {resolved}"
        if replaced:
            msg += f" (auto-sanitized {replaced} non-ASCII chars)"
        return msg
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


_BLOCKED_GIT_SUBCOMMANDS = {"checkout", "reset", "revert", "clean", "stash", "restore", "push", "pull"}

# Commands that are always blocked — too dangerous for an autonomous coding agent.
# NOTE: Multi-word entries use substring matching, so only put exact dangerous commands here.
# Inline code patterns (python -c, sh -c, etc.) are handled separately to avoid false positives.
# Shell interpreters (bash, sh, zsh) are NOT here — inline patterns block dangerous -c usage,
# and file-execution ("bash script.sh") is a legitimate Ralph workflow.
_BLOCKED_COMMANDS = {
    # Network / remote access
    "curl", "wget", "ssh", "scp", "sftp", "rsync",
    "nc", "ncat", "netcat", "telnet",
    "mosquitto_pub", "mosquitto_sub",
    # OS / app interaction
    "osascript", "open", "xdg-open", "mimeopen",
    "launchctl", "systemctl", "service",
    # Destructive filesystems
    "rm", "rmdir", "dd", "truncate", "mkfs",
    "fdisk", "parted", "mount", "umount",
    # Privilege escalation
    "sudo", "su",
    # Package managers (install arbitrary code)
    "pip install", "pip3 install", "npm install", "yarn",
    "gem install", "cargo install", "go install", "apk",
    # Interactive editors (Ralph should use write_file, not interactive editors)
    "vim", "vi", "nano", "emacs",
    # Misc dangerous single tokens
    "chmod", "chown", "exit",
    #expect is handled by inline pattern
}

# Commands that are allowed (first token must be in this set or match git subcommand pattern)
_ALLOWED_COMMANDS = {
    "python3", "python", "git", "grep", "find", "cat", "head", "tail",
    "wc", "jq", "sort", "uniq", "awk", "sed", "ls", "pwd", "echo",
    "test", "mkdir", "touch", "cp", "mv", "diff", "md5sum", "sha256sum",
    "sha1sum", "sha512sum",
    "xargs", "tr", "cut", "basename", "dirname", "realpath", "readlink",
    "stat", "file", "tree", "fold", "printf", "date", "time",
    "git status", "git log", "git diff", "git add", "git commit", "git show",
    "git branch", "git checkout", "git reset", "git revert",
    "git stash list", "git stash pop", "git stash drop",
    "git config", "git remote -v", "git fetch", "git describe",
    "git rev-parse", "git symbolic-ref",
    "python3 -m", "python -m",  # module invocation only
    "pip list", "pip show",     # read-only pip queries
    "pip3 list", "pip3 show",
    # Shell interpreters (safe when running files; dangerous -c usage blocked by inline patterns)
    "bash", "sh", "zsh", "dash",
    # Terminal session recorder (safe when running files)
    "script",
}


def _is_command_blocked(command: str) -> tuple[bool, str]:
    """
    Check if a command is blocked.
    Returns (blocked: bool, reason: str).
    """
    import re as _re

    # Check exact blocked phrases (multi-word)
    for blocked in _BLOCKED_COMMANDS:
        if blocked in command and not blocked.startswith("git "):
            # Skip git subcommands — handled separately
            return True, blocked

    # Block inline code execution patterns (allowlist approach to dangerous flags)
    # Block dangerous flag patterns while allowing safe file/script invocations
    inline_code_patterns = [
        r'python3?\s+-(?!m\b|[d-z-])([c-]|$)',   # python -c, python3 -c, python -; NOT -m, -V, -h, --*
        r'\bruby\s+-[er]',                        # ruby -e, ruby -r
        r'\bperl\s+-[e]',                         # perl -e
        r'\bnode\s+-[er]',                        # node -e, node --eval
        r'\b(bash|sh|zsh|dash)\s+-c\b',           # bash -c inline; NOT "bash script.sh"
        r'\bexpect\b',
    ]
    for pattern in inline_code_patterns:
        if _re.search(pattern, command):
            return True, "inline code execution"

    # Allowlist: check first token (strip leading ./ and /usr/bin/ and $ prefixes)
    first_token_match = _re.search(r'^\s*(?:(?:\./?)|(?:/usr(?:/bin)?/)?|(?:\$))?([a-zA-Z0-9_-]+)', command)
    if first_token_match:
        first_token = first_token_match.group(1)
        # git is handled by its own block
        if first_token == "git":
            return False, ""  # let git handler deal with it
        if first_token not in _ALLOWED_COMMANDS:
            return True, first_token

    return False, ""


def tool_run_command(command: str, cwd: str = None, timeout: int = 60) -> str:
    work_dir = _resolve_path(cwd) if cwd else WORKSPACE

    # Block destructive/personal git subcommands - workspace git is a local mirror (never push/pull)
    import re as _re
    _git_sub = _re.search(r'\bgit\s+(\w+)', command)
    if _git_sub and _git_sub.group(1) in _BLOCKED_GIT_SUBCOMMANDS:
        blocked = _git_sub.group(1)
        log.warning(f"run_command BLOCKED: 'git {blocked}' is not allowed. Use git_commit to save work.")
        return f"ERROR: 'git {blocked}' is blocked. Ralph may only use git_status and git_commit. Do not push or pull - workspace git is a local mirror only."

    # Command allowlist / blocklist check
    blocked, reason = _is_command_blocked(command)
    if blocked:
        log.warning(f"run_command BLOCKED: '{reason}' is not allowed in autonomous mode.")
        return f"ERROR: '{reason}' is not an allowed command. Use only safe read-only or code-analysis commands. If you need a specific tool, ask your manager to add it to the allowlist."

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
    # Sanitize commit message - same Unicode problem as write_file
    clean_msg, replaced = _sanitize_for_python(message, 'msg.py')  # reuse sanitizer
    if replaced:
        log.warning(f"git_commit: sanitized {replaced} non-ASCII chars from commit message")
    message = clean_msg
    log.info(f"git_commit: {message!r}")
    # Check if there are any changes to commit
    status_result = tool_run_command('git status --porcelain', cwd=str(WORKSPACE))
    if not status_result.strip():
        # Nothing to commit - already committed or no changes
        return "OK: Nothing to commit (working tree clean)"
    for _git_attempt in range(3):
        result = tool_run_command(f'git add -A && git commit -m {json.dumps(message)}', cwd=str(WORKSPACE))
        if 'EXIT CODE: 0' in result:
            return result
        log.warning(f'Git commit attempt {_git_attempt+1}/3 failed, retrying in 3s...')
        import time as _time; _time.sleep(3)
    return result  # return final result after 3 attempts


def tool_task_complete(summary: str) -> str:
    """Signals completion - handled by ralph.py, not actually executed here."""
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
