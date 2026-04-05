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
    """
    Normalize problematic Unicode in .py files.
    - Smart quotes → ASCII equivalents (prevents bugs in string literals)
    - Em/en dashes → hyphens
    - Non-breaking spaces → regular spaces (prevents indentation bugs)
    - Other Unicode is PASSED THROUGH — Python 3 handles UTF-8 natively.
    Returns (content, count_replaced).
    """
    if not path.endswith('.py'):
        return content, 0
    result = content
    count = 0
    for bad, good in _UNICODE_SUBS.items():
        n = result.count(bad)
        if n:
            result = result.replace(bad, good)
            count += n
    # NOTE: Do NOT replace remaining non-ASCII with '_'.
    # Python 3 fully supports Unicode in identifiers, strings, and comments.
    # Silently destroying Unicode (e.g. in docstrings, comments, string literals)
    # corrupts the source. If encoding is a problem, write_text() will error — which
    # is the correct behavior, not silent corruption.
    return result, count


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
    # NOTE: curl and wget are ALLOWED here (in _ALLOWED_COMMANDS) for public URL fetching.
    # They remain in this comment for documentation only — do NOT add them back to _BLOCKED.
    "ssh", "scp", "sftp", "rsync",
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
    # Rust toolchain (safe build/test tools for workspace projects)
    "cargo", "cargo build", "cargo test", "cargo check", "cargo clippy",
    "rustc", "rustfmt",
    # Network fetchers (read-only public URLs for Ralph's research and critique stages)
    "curl", "wget",
    # Low-level system utilities (safe read-only use for git operations)
    "stty",               # git commit fallback; dd removed — too dangerous
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

    # Check blocked commands using word-boundary regex for short tokens (<=3 chars).
    # Using plain substring matching (e.g. "nc" in "NANSEN_MOCK") is too loose —
    # it breaks compound commands like "test -f x || echo y" (|| contains "nc").
    # Short tokens: use \b word boundary so "nc" doesn't match "NANSEN" or "||"
    # Long tokens: substring matching is fine (e.g. "launchctl" won't appear in random paths)
    #
    # Exceptions:
    # - "vi" skipped: \bvi\b matches "vi" in "git commit -vi" (verbose flag),
    #   which is a false positive. vi is already blocked via _ALLOWED_COMMANDS
    #   anyway (Ralph can't run interactive editors), so no safety gap.
    _SHORT_BLOCKERS = {b for b in _BLOCKED_COMMANDS if len(b) <= 3}
    for blocked in _BLOCKED_COMMANDS:
        if blocked.startswith("git "):
            continue  # Skip git subcommands — handled separately
        if blocked in _SHORT_BLOCKERS:
            if blocked == "vi":
                # Special case: don't block "vi" in "git commit -vi" (verbose flag).
                # Use negative lookbehind to require that vi is NOT preceded by a hyphen.
                # (?<![a-zA-Z0-9_-])vi\b matches " vi " but NOT "-vi"
                if _re.search(r'(?<![a-zA-Z0-9_-])vi\b', command):
                    return True, blocked
            elif _re.search(rf'\b{_re.escape(blocked)}\b', command):
                return True, blocked
        else:
            # Substring match for longer tokens (launchctl, mosquitto_*, etc.)
            if blocked in command:
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

    # Allowlist: check first token.
    # First, strip leading VAR=value assignments (env vars don't affect safety assessment).
    # e.g. "NANSEN_MOCK=1 python3 script.py" -> first token is "python3"
    # Also handles: ./script.sh, /usr/bin/python3, $VAR, ${VAR}
    stripped = command
    while True:
        m = _re.match(r'^[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+', stripped)
        if not m:
            break
        stripped = stripped[m.end():]

    first_token_match = _re.search(r'^([a-zA-Z_./$-][a-zA-Z0-9_./$-]*)', stripped)
    if first_token_match:
        first_token = first_token_match.group(1)
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
    # Sanitize commit message - strip C0/C1 control characters only.
    # Preserve valid Unicode: accented letters (e.g. café, naïve), CJK, emoji in commit
    # messages are valid in UTF-8 git. We only strip the 33 control chars (0x00-0x1F
    # except TAB/LF/CR) that can break git or shell tools.
    _CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    clean = _CONTROL.sub('', message)
    if clean != message:
        replaced = sum(1 for c in message if ord(c) < 32 and c not in '\t\n\r')
        log.warning(f"git_commit: stripped {replaced} control chars from commit message")
    message = clean
    log.info(f"git_commit: {message!r}")
    # Check if there are any changes to commit
    status_result = tool_run_command('git status --porcelain', cwd=str(WORKSPACE))
    if not status_result.strip():
        # Nothing to commit - already committed or no changes
        return "OK: Nothing to commit (working tree clean)"
    # Write message to temp file (Python I/O, no shell interpretation)
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile(mode='w', suffix='.msg', delete=False, prefix='git_commit_') as _f:
        _f.write(message)
        _msg_path = _f.name
    try:
        result = tool_run_command(f'git add -A && git commit -F {json.dumps(_msg_path)}', cwd=str(WORKSPACE))
        if 'EXIT CODE: 0' in result:
            return result
        log.warning(f'Git commit attempt 1/3 failed, retrying in 3s...')
        import time as _time; _time.sleep(3)
        result = tool_run_command(f'git add -A && git commit -F {json.dumps(_msg_path)}', cwd=str(WORKSPACE))
        if 'EXIT CODE: 0' in result:
            return result
        log.warning(f'Git commit attempt 2/3 failed, retrying in 3s...')
        _time.sleep(3)
        result = tool_run_command(f'git add -A && git commit -F {json.dumps(_msg_path)}', cwd=str(WORKSPACE))
        return result
    finally:
        import os as _os
        try: _os.unlink(_msg_path)
        except: pass


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
