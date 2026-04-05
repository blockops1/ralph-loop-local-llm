#!/bin/bash
# ralph.sh — Ralph Docker wrapper
#
# Wraps docker compose so the Mac Mini host can run Ralph without
# having Python or any Ralph dependencies installed locally.
#
# Default: 3-stage pipeline (CREATE -> CRITIQUE -> FIX) per story.
# Use --single-stage to disable the 3-stage pipeline.
#
# Usage:
#   ./ralph.sh <slug>                    # 3-stage pipeline (standard)
#   ./ralph.sh <slug> --single-stage    # single-stage (CREATE only)
#   ./ralph.sh <slug> --story US-001    # single story, 3-stage
#   ./ralph.sh --list-projects           # list all projects

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure image is up to date
docker compose build --quiet

exec docker compose run --rm \
  --entrypoint "python3 ralph.py" \
  ralph "$@"
