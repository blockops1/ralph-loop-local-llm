#!/bin/bash
# ralph.sh — Ralph Loop shell wrapper
#
# Scans ralph/projects/ for the first active project (incomplete stories,
# no lock) and runs ralph.py on it. Safe to call from cron.
#
# Usage:
#   ./ralph/ralph.sh                    # auto-pick first active project
#   ./ralph/ralph.sh <slug>             # run specific project
#   ./ralph/ralph.sh <slug> --dry-run   # dry run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="python3"
RALPH_PY="$SCRIPT_DIR/ralph.py"
PROJECTS_DIR="$SCRIPT_DIR/projects"

# Activate virtualenv if present
if [ -f "$WORKSPACE_DIR/.venv/bin/activate" ]; then
    source "$WORKSPACE_DIR/.venv/bin/activate"
fi

# Ensure dependencies
if ! $PYTHON -c "import yaml, requests" 2>/dev/null; then
    echo "Installing dependencies..."
    $PYTHON -m pip install pyyaml requests --quiet
fi

# Determine slug
SLUG="${1:-}"
EXTRA_ARGS="${@:2}"

# Handle --critique flag stub
CRITIQUE_MODE=0
if [ "$1" = "--critique" ]; then
    CRITIQUE_MODE=1
    SLUG="${2:-}"
    EXTRA_ARGS="${@:3}"
fi

if [ -z "$SLUG" ]; then
    # Auto-detect: find first project with pending stories and no active lock
    SLUG=$($PYTHON - <<'EOF'
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from prd_manager import list_active_projects
projects = list_active_projects()
if projects:
    print(projects[0])
else:
    sys.exit(1)
EOF
)
    if [ -z "$SLUG" ]; then
        echo "No active Ralph projects found."
        exit 0
    fi
    echo "Auto-selected project: $SLUG"
fi

# Critique mode implementation
if [ "$CRITIQUE_MODE" = "1" ]; then
    PRD_PATH="$PROJECTS_DIR/$SLUG/prd.json"
    if [ ! -f "$PRD_PATH" ]; then
        echo "Error: prd.json not found at $PRD_PATH"
        exit 1
    fi
    git -C "$WORKSPACE_DIR" diff HEAD -- . > "/tmp/ralph-diff-$SLUG.txt"
    if [ ! -s "/tmp/ralph-diff-$SLUG.txt" ]; then
        echo "No changes to critique"
        exit 0
    fi
    PROMPT_FILE="$SCRIPT_DIR/PROMPT-critique.md"
    if [ ! -f "$PROMPT_FILE" ]; then
        echo "Error: PROMPT-critique.md not found at $PROMPT_FILE"
        exit 1
    fi
    OUTPUT_FILE="$PROJECTS_DIR/ralph-test/critique.md"
    $PYTHON - <<EOF
import json
import requests

system_prompt = open("$PROMPT_FILE").read()
prd_desc = json.dumps(json.load(open("$PRD_PATH")), indent=2)
diff_content = open("/tmp/ralph-diff-$SLUG.txt").read()
user_message = f"Diff content:\n\n{diff_content}\n\nPRD description:\n\n{prd_desc}"

resp = requests.post(
    "http://localhost:11434/chat/completions",
    json={
        "model": "Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "stream": False
    },
    timeout=300
)
resp.raise_for_status()
result = resp.json()
output = result["choices"][0]["message"]["content"]

with open("$OUTPUT_FILE", "w") as f:
    f.write(output)

print(f"Critique written to $OUTPUT_FILE")
EOF
    exit 0
fi

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
# Per-run timestamped log — avoids multiple cron runs colliding in the same file
LOG_FILE="$LOG_DIR/${SLUG}-$(date +%Y-%m-%dT%H%M%S).log"

# CPU governor: flip to performance for duration of run, restore on exit
_set_governor() {
    local gov="$1"
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo "$gov" | sudo tee "$f" > /dev/null 2>&1 || true
    done
    echo "CPU governor → $gov"
}
PREV_GOVERNOR="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo ondemand)"
trap '_set_governor "$PREV_GOVERNOR"' EXIT
_set_governor performance

echo "=== Ralph Loop: $SLUG ===" >> "$LOG_FILE"
cd "$WORKSPACE_DIR"

# Load credentials so ralph.py has DISCORD_BOT_TOKEN etc in environment
if [ -f "$HOME/.hermes/.env" ]; then
    set -a
    source "$HOME/.hermes/.env"
    set +a
fi

# Run detached — nohup ensures ralph survives even if the caller/session is killed
nohup $PYTHON "$RALPH_PY" "$SLUG" $EXTRA_ARGS >> "$LOG_FILE" 2>&1 &
RALPH_PID=$!
echo "Ralph started: PID=$RALPH_PID log=$LOG_FILE"

# Wait for Ralph to finish so sequential callers don't stack parallel runs
wait $RALPH_PID
RALPH_EXIT=$?
echo "Ralph finished: PID=$RALPH_PID exit=$RALPH_EXIT"

# Rotate logs — keep last 20 per project, delete older
ls -t "$LOG_DIR"/${SLUG}-*.log 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true

# Auto-critique: run critique agent after successful completion
if [ "${AUTO_CRITIQUE:-1}" != "0" ] && [ "$RALPH_EXIT" -eq 0 ]; then
    echo "[CRITIQUE] Running post-completion critique..."
    bash "$SCRIPT_DIR/ralph.sh" --critique "$SLUG"
    CRITIQUE_EXIT=$?
    if [ $CRITIQUE_EXIT -eq 0 ] && [ -f "$SCRIPT_DIR/projects/$SLUG/critique.md" ]; then
        python3 "$SCRIPT_DIR/scripts/critique_to_stories.py" \
            "$SCRIPT_DIR/projects/$SLUG/critique.md" \
            "$SCRIPT_DIR/projects/$SLUG/prd.json"
        if [ $? -eq 0 ]; then
            echo "[CRITIQUE] Rework stories added — re-running Ralph"
            AUTO_CRITIQUE=0 bash "$SCRIPT_DIR/ralph.sh" "$SLUG"
        fi
    fi
fi
