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

if [ -z "$SLUG" ]; then
    # Auto-detect: find first project with pending stories and no active lock
    SLUG=$($PYTHON - <<'EOF'
import sys
sys.path.insert(0, "/Users/yourname/workspace/ralph")
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

# Load credentials so ralph.py has TELEGRAM_BOT_TOKEN etc in environment
if [ -f "$HOME/.env" ]; then
    source "$HOME/.env"
elif [ -f "$HOME/.openclaw/.env" ]; then
    set -a
    source "$HOME/.openclaw/.env"
    set +a
fi

# Run detached — nohup ensures ralph survives even if the caller/session is killed
nohup $PYTHON "$RALPH_PY" "$SLUG" $EXTRA_ARGS >> "$LOG_FILE" 2>&1 &
RALPH_PID=$!
echo "Ralph started: PID=$RALPH_PID log=$LOG_FILE"

# Rotate logs — keep last 20 per project, delete older
ls -t "$LOG_DIR"/${SLUG}-*.log 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true
