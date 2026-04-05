#!/bin/bash
# ralph.sh — Ralph Docker wrapper
#
# Wraps docker compose so the Mac Mini host can run Ralph without
# having Python or any Ralph dependencies installed locally.
#
# Usage:
#   ./ralph.sh                    # auto-pick first active project
#   ./ralph.sh <slug>             # run specific project
#   ./ralph.sh <slug> --story US-001   # run specific story
#   ./ralph.sh --list-projects    # list all projects

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure image is up to date
docker compose build --quiet

# Run pipeline_runner.py via docker compose
# Override entrypoint to get clean arg passing
exec docker compose run --rm \
  --entrypoint "python3 pipeline_runner.py" \
  ralph "$@"
