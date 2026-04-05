# Ralph Loop — Docker Edition

Autonomous coding loop that runs a local LLM (Qwen 3.5 27B via llama-server) through structured coding tasks defined in `prd.json`.

## Architecture

Ralph runs **inside a Docker container** with minimal blast radius. It can only write to mounted volumes.

```
host (Mac Mini)
  ├── llama-server (port 8090) ← LLM backend
  └── ~/ralph/                  ← source + config
        ├── docker compose       ← runs Ralph
        └── ralph.sh            ← host wrapper

container (Ralph)
  ├── /app/projects/            ← PRDs + code (volume mounted)
  ├── /app/logs/               ← pipeline logs (volume mounted)
  └── /app/config.yaml         ← model URL pointing to host.docker.internal:8090
```

## Prerequisites

- Docker Desktop running on Mac Mini
- llama-server running on Mac Mini host at `http://localhost:8090`
- `~/.hermes/.env` with `DISCORD_BOT_TOKEN` and/or `TELEGRAM_BOT_TOKEN`

## Quick Start

```bash
cd ~/ralph

# Build image (one time)
docker compose build

# Run pipeline — auto-selects first active project
./ralph.sh

# Run a specific project
./ralph.sh my-project-slug
```

## Project Data

Projects live in Docker named volumes. To access them from the host:

```bash
ls ~/ralph-data/projects/          # host path for projects volume
docker compose run --rm ralph ls /app/projects/
```

## Configuration

`config.yaml` is baked into the image at build time. To override per deployment:

```yaml
# docker-compose.yml already mounts ./config.yaml as read-only
# Replace ./config.yaml with your deployment-specific config
```

Key setting: `model_url` must point to the host's llama-server:

- **From host shell** (outside container): `http://localhost:8090/v1`
- **From inside container**: `http://host.docker.internal:8090/v1`

## Projects Directory

Each project lives at `~/ralph/projects/{slug}/`:

```
{slug}/
├── prd.json          ← source of truth (must exist)
├── progress.txt      ← append-only run log
├── critique.md       ← stage 2 output
├── code/             ← generated code
└── .ralph.lock       ← concurrency lock
```

## Troubleshooting

```bash
# Check if Docker Desktop is running
docker info

# See latest pipeline log
docker compose run --rm ralph tail -f /app/logs/$(ls -t /app/logs/ | head -1)

# Verify llama-server is up
curl -s http://127.0.0.1:8090/v1/models

# Reset a blocked story
docker compose run --rm ralph python3 -c "
import json, sys
d = json.load(open('/app/projects/YOUR_SLUG/prd.json'))
for s in d['userStories']:
    if s['id'] == 'US-XXX':
        s['passes'] = False
        s['attempts'] = 0
        s.pop('status', None)
        s.pop('error', None)
json.dump(d, open('/app/projects/YOUR_SLUG/prd.json','w'), indent=2)
print('Reset:', 'US-XXX')
"

# List all projects
docker compose run --rm ralph python3 pipeline_runner.py --list-projects
```
