#!/bin/bash
# ralph.sh — Ralph Docker wrapper
#
# Wraps docker compose so the Mac Mini host can run Ralph without
# having Python or any Ralph dependencies installed locally.
#
# Uses ralph.py (loop mode) — runs ALL pending stories until done, blocked,
# or max_iterations reached. For single-story 3-stage pipeline, use:
#   docker compose run --rm ralph python3 pipeline_runner.py <slug>
#
# Usage:
#   ./ralph.sh                    # auto-pick first active project
#   ./ralph.sh <slug>             # run specific project (all pending stories)
#   ./ralph.sh <slug> --story US-001   # run specific story only
#   ./ralph.sh --list-projects    # list all projects

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure image is up to date
docker compose build --quiet

# Run ralph.py via docker compose (loop mode — all stories until done)
exec docker compose run --rm \
  --entrypoint "python3 ralph.py" \
  ralph "$@"
