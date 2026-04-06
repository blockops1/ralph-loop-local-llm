"""
tools.py - Ralph Loop tool definitions and executors.

Provides:
  - TOOL_DEFINITIONS: OpenAI-format tool specs to pass in each API request
  - execute_tool(name, args, cwd): dispatches tool calls to actual functions
"""

import os
import re
import subprocess
import json
import logging
import tempfile
from pathlib import Path

log = logging.getLogger("ralph.tools")

WORKSPACE = Path(__file__).resolve().parent


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
                    "path": {"type": "string", "description": "Path to file, relative to workspace root or absolute."}
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
                    "path": {"type": "string", "description": "Path to file, relative to workspace root or absolute."},
                    "content": {"type": "string", "description": "Full content to write to the file."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": "Copy a file from source to destination. Both paths are interpreted relative to workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Source file path."},
                    "dst": {"type": "string", "description": "Destination file path."}
                },
                "required": ["src", "dst"]
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
                    "path": {"type": "string", "description": "Directory path, relative to workspace root or absolute."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a pattern in files (like grep). Returns matching lines with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search."},
                    "path": {"type": "string", "description": "Directory to search in. Defaults to workspace root."},
                    "file_glob": {"type": "string", "description": "Optional glob pattern to filter files (e.g. '*.py')."}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return stdout, stderr, and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    "cwd": {"type": "string", "description": "Working directory (optional)."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run pytest on a directory or file. Returns test results summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to test file or directory (relative to workspace)."},
                    "args": {"type": "string", "description": "Additional pytest args as a string."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Fetch a URL and return the response body. Use for API calls, fetching docs, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch."},
                    "headers": {"type": "object", "description": "Optional HTTP headers as a dict."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_json",
            "description": "Query data from a JSON file using a JMESPath-like key path (e.g. 'data.tokens[0].address').",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to JSON file."},
                    "query": {"type": "string", "description": "JMESPath query."}
                },
                "required": ["path", "query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Check git status — shows modified, staged, and untracked files.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes and create a LOCAL git commit. Workspace git is a mirror only — NO push/pull.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message."}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Signal that the current story is fully implemented.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Brief summary of what was done."}
                },
                "required": ["summary"]
            }
        }
    }
]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _resolve_path(path: str) -> Path:
    """Resolve path relative to workspace if not absolute."""
    p = Path(path)
    if p.is_absolute():
        return p
    return WORKSPACE / p


# ---------------------------------------------------------------------------
# Unicode sanitizer for Python files
# ---------------------------------------------------------------------------

_UNICODE_SUBS = {
    '\u2014': '-', '\u2013': '-', '\u2018': "'", '\u2019': "'",
    '\u201c': '"', '\u201d': '"', '\u2192': '->', '\u2022': '*',
    '\u00b7': '.', '\u2026': '...', '\u00a0': ' ',
}

def _sanitize_for_python(content: str, path: str) -> tuple[str, int]:
    if not path.endswith('.py'):
        return content, 0
    result = content
    count = 0
    for bad, good in _UNICODE_SUBS.items():
        n = result.count(bad)
        if n:
            result = result.replace(bad, good)
            count += n
    return result, count


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

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


def tool_write_file(path: str, content: str) -> str:
    _TRUNCATION_MARKERS = [
        "... [truncated", "[truncated -", "# ... truncated", "# [truncated",
    ]
    for _marker in _TRUNCATION_MARKERS:
        if _marker in content:
            log.error(f"write_file BLOCKED: truncation artifact detected for {path}")
            return (
                f"ERROR: Content contains a truncation artifact ({_marker!r}). "
                f"Do NOT write truncated content. Re-read the file to get the complete content."
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


def tool_copy_file(src: str, dst: str) -> str:
    """Copy src to dst. dst's parent dir is created if needed."""
    src_p = _resolve_path(src)
    dst_p = _resolve_path(dst)
    if not src_p.exists():
        return f"ERROR: Source not found: {src_p}"
    log.info(f"copy_file: {src_p} -> {dst_p}")
    try:
        import shutil
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, dst_p)
        return f"OK: Copied {src} -> {dst}"
    except Exception as e:
        return f"ERROR copying file: {e}"


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


# ---------------------------------------------------------------------------
# Search tools
# ---------------------------------------------------------------------------

def tool_search_files(pattern: str, path: str = None, file_glob: str = None) -> str:
    """Search for pattern in files. Returns matching lines with line numbers."""
    search_root = _resolve_path(path) if path else WORKSPACE
    log.info(f"search_files: pattern={pattern!r} path={search_root}")

    cmd = ["grep", "-rn", "--color=never", pattern, str(search_root)]
    if file_glob:
        cmd.insert(2, "--include")
        cmd.insert(3, file_glob)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.stdout.strip():
            return result.stdout.strip()
        return f"(no matches for {pattern!r} in {search_root})"
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out after 30s"
    except Exception as e:
        return f"ERROR searching files: {e}"


# ---------------------------------------------------------------------------
# Shell / command tools
# ---------------------------------------------------------------------------

_BLOCKED_GIT_SUBCOMMANDS = {"checkout", "reset", "revert", "clean", "stash", "restore", "push", "pull"}
_BLOCKED_COMMANDS = {
    "ssh", "scp", "sftp", "rsync", "telnet",
    "mosquitto_pub", "mosquitto_sub",
    "osascript", "open", "xdg-open", "mimeopen", "launchctl", "systemctl", "service",
    "rm", "rmdir", "dd", "truncate", "mkfs", "fdisk", "parted", "mount", "umount",
    "sudo", "su",
    "pip install", "pip3 install", "npm install", "yarn", "gem install", "cargo install", "go install", "apk",
    "vim", "vi", "nano", "emacs",
    "chmod", "chown",
}
_ALLOWED_COMMANDS = {
    "python3", "python", "git", "grep", "find", "cat", "head", "tail",
    "wc", "jq", "sort", "uniq", "awk", "sed", "ls", "pwd", "echo", "test",
    "mkdir", "touch", "cp", "mv", "diff", "md5sum", "sha256sum", "sha1sum", "sha512sum",
    "xargs", "tr", "cut", "basename", "dirname", "realpath", "readlink",
    "stat", "file", "tree", "fold", "printf", "date", "time",
    "git status", "git log", "git diff", "git add", "git commit", "git show",
    "git branch", "git checkout", "git reset", "git revert",
    "git stash list", "git stash pop", "git stash drop",
    "git config", "git remote -v", "git fetch", "git describe", "git rev-parse",
    "python3 -m", "python -m",
    "pip list", "pip show", "pip3 list", "pip3 show",
    "cargo", "cargo build", "cargo test", "cargo check", "cargo clippy",
    "rustc", "rustfmt",
    "curl", "wget",
    "stty", "bash", "sh", "zsh", "dash", "script",
    "pytest", "python3 -m pytest", "py.test",
    "cd", "pwd",
}


def _is_command_blocked(command: str) -> tuple[bool, str]:
    import re as _re

    _SHORT_BLOCKERS = {b for b in _BLOCKED_COMMANDS if len(b) <= 3}
    for blocked in _BLOCKED_COMMANDS:
        if blocked.startswith("git "):
            continue
        if blocked in _SHORT_BLOCKERS:
            if blocked == "vi":
                if _re.search(r'(?<![a-zA-Z0-9_-])vi\b', command):
                    return True, blocked
            elif _re.search(rf'\b{_re.escape(blocked)}\b', command):
                return True, blocked
        else:
            if blocked in command:
                return True, blocked

    inline_code_patterns = [
        r'python3?\s+-(?!m\b|[d-z-])([c-]|$)',
        r'\bruby\s+-[er]', r'\bperl\s+-[e]', r'\bnode\s+-[er]',
        r'\b(bash|sh|zsh|dash)\s+-c\b', r'\bexpect\b',
    ]
    for pattern in inline_code_patterns:
        if _re.search(pattern, command):
            return True, "inline code execution"

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

    _git_sub = re.search(r'\bgit\s+(\w+)', command)
    if _git_sub and _git_sub.group(1) in _BLOCKED_GIT_SUBCOMMANDS:
        return f"ERROR: 'git {_git_sub.group(1)}' is blocked. Use git_status and git_commit only."

    blocked, reason = _is_command_blocked(command)
    if blocked:
        return f"ERROR: '{reason}' is not an allowed command."

    log.info(f"run_command: {command!r} (cwd={work_dir})")
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=str(work_dir), timeout=timeout,
        )
        parts = []
        if result.stdout.strip():
            parts.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()}")
        parts.append(f"EXIT CODE: {result.returncode}")
        return "\n".join(parts) if parts else f"EXIT CODE: {result.returncode}"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR running command: {e}"


def tool_run_tests(path: str = None, args: str = "") -> str:
    """Run pytest on a test directory or file."""
    test_path = _resolve_path(path) if path else WORKSPACE
    cmd = f"pytest {test_path}"
    if args:
        cmd += f" {args}"
    return tool_run_command(cmd, timeout=120)


def tool_http_get(url: str, headers: dict = None) -> str:
    """Fetch a URL using curl."""
    import urllib.request
    log.info(f"http_get: {url}")
    try:
        req = urllib.request.Request(url)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            if len(content) > 50000:
                return content[:50000] + f"\n... [truncated {len(content)-50000} chars]"
            return content
    except Exception as e:
        return f"ERROR fetching {url}: {e}"


def tool_query_json(path: str, query: str) -> str:
    """Query a JSON file using a simple dot/brackets path."""
    resolved = _resolve_path(path)
    if not resolved.exists():
        return f"ERROR: File not found: {resolved}"
    try:
        with open(resolved) as f:
            data = json.load(f)
    except Exception as e:
        return f"ERROR loading JSON: {e}"

    # Simple JMESPath-like query: 'a.b.c[0].d' or 'a.b[1]'
    parts = re.split(r'\.(?![^\[]*\])', query)
    try:
        for part in parts:
            m = re.match(r'^(.+)\[(-?\d+)\]$', part)
            if m:
                key, idx = m.group(1), int(m.group(2))
                if key:
                    data = data[key]
                data = data[idx]
            else:
                data = data[part]
        return json.dumps(data, indent=2, ensure_ascii=False)
    except (KeyError, IndexError, TypeError) as e:
        return f"ERROR: Query '{query}' failed at '{part}': {e}"


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------

def tool_git_status() -> str:
    log.info("git_status")
    return tool_run_command("git status --short && git log --oneline -5", cwd=str(WORKSPACE))


def tool_git_commit(message: str) -> str:
    _CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    clean = _CONTROL.sub('', message)
    if clean != message:
        log.warning(f"git_commit: stripped control chars from message")
    message = clean

    status_result = tool_run_command('git status --porcelain', cwd=str(WORKSPACE))
    if not status_result.strip():
        return "OK: Nothing to commit (working tree clean)"

    with tempfile.NamedTemporaryFile(mode='w', suffix='.msg', delete=False, prefix='git_commit_') as _f:
        _f.write(message)
        _msg_path = _f.name
    try:
        result = tool_run_command(f'git add -A && git commit -F {json.dumps(_msg_path)}', cwd=str(WORKSPACE))
        if 'EXIT CODE: 0' in result:
            return result
        import time as _time
        for attempt in range(2):
            _time.sleep(3)
            result = tool_run_command(f'git add -A && git commit -F {json.dumps(_msg_path)}', cwd=str(WORKSPACE))
            if 'EXIT CODE: 0' in result:
                return result
        return result
    finally:
        try:
            os.unlink(_msg_path)
        except:
            pass


def tool_task_complete(summary: str) -> str:
    log.info(f"task_complete: {summary[:100]}")
    return f"TASK_COMPLETE: {summary}"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

EXECUTORS = {
    "read_file":     lambda a: tool_read_file(a["path"]),
    "write_file":    lambda a: tool_write_file(a["path"], a["content"]),
    "copy_file":     lambda a: tool_copy_file(a["src"], a["dst"]),
    "list_dir":      lambda a: tool_list_dir(a["path"]),
    "search_files":  lambda a: tool_search_files(a["pattern"], a.get("path"), a.get("file_glob")),
    "run_command":   lambda a: tool_run_command(a["command"], a.get("cwd")),
    "run_tests":     lambda a: tool_run_tests(a.get("path"), a.get("args", "")),
    "http_get":      lambda a: tool_http_get(a["url"], a.get("headers")),
    "query_json":    lambda a: tool_query_json(a["path"], a["query"]),
    "git_status":    lambda a: tool_git_status(),
    "git_commit":    lambda a: tool_git_commit(a["message"]),
    "task_complete": lambda a: tool_task_complete(a["summary"]),
}


def execute_tool(name: str, args: dict) -> str:
    if name not in EXECUTORS:
        return f"ERROR: Unknown tool '{name}'. Available: {list(EXECUTORS.keys())}"
    try:
        return EXECUTORS[name](args)
    except KeyError as e:
        return f"ERROR: Missing required argument {e} for tool '{name}'"
    except Exception as e:
        log.exception(f"Tool '{name}' raised exception")
        return f"ERROR in tool '{name}': {e}"
