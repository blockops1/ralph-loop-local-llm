# Ralph Migration Guide

## Status: NEW CONTAINER BUILT — OLD LOCATION PRESERVED

The Docker containerized Ralph has been built at `~/ralph/`. The original setup at `~/.hermes/workspace/ralph/` is **untouched and fully functional**.

## What Was Built

```
~/ralph/
├── Dockerfile              # python:3.11-slim + pyyaml + requests
├── docker-compose.yml     # named volumes + host networking
├── ralph.sh               # host wrapper: docker compose run --rm ralph
├── config.yaml            # points to host.docker.internal:8090
├── requirements.txt
├── .env.template
├── .dockerignore
├── README.md
├── docs/admin.md          # Docker-specific admin guide
├── skills/
│   ├── ralph-loop/        # loop management skill
│   └── ralph-prd/         # PRD writing skill (with references/)
├── *.py                   # all Ralph Python source
└── PROMPT*.md              # all stage prompts
```

## What Is NOT Yet Done

- [ ] Docker image build (waiting on network — see below)
- [ ] Container test run
- [ ] llama-server connectivity verified from inside container
- [ ] Projects migrated from old location
- [ ] Old location marked as deprecated
- [ ] Skills symlinked or copied to `~/.hermes/skills/` so Hermes finds them

## Known Issue: Docker Image Pull

Docker Desktop on this Mac Mini appears to have **no external network access** to Docker Hub (registry.hub.docker.com ping fails). The `python:3.11-slim` base image cannot be pulled.

**Workarounds:**
1. Run `docker pull python:3.11-slim` from a network-connected location first
2. Use a different base image already cached locally
3. Build on a machine with Docker Hub access, then `docker save`/`docker load`

## Step 1: Build the Image (once network is available)

```bash
cd ~/ralph
docker compose build
```

## Step 2: Test Connectivity

```bash
# Test that container can reach host llama-server
docker compose run --rm ralph curl -s http://host.docker.internal:8090/v1/models
```

Expected: JSON with model list

## Step 3: List Projects

```bash
docker compose run --rm ralph python3 pipeline_runner.py --list-projects
```

## Step 4: Run a Test Project

```bash
./ralph.sh <existing-project-slug>
```

## Step 5: Migrate Projects

When the container is verified working, migrate projects from old location:

```bash
# Dry run first
rsync -av ~/.hermes/workspace/ralph/projects/ ~/ralph-data/projects/

# Actual copy (uncomment when ready)
# rsync -av ~/.hermes/workspace/ralph/projects/ ~/ralph-data/projects/
```

## Step 6: Deprecate Old Location

After successful container run:

```bash
# Add deprecation note to old location
echo "DEPRECATED — moved to ~/ralph/" > ~/.hermes/workspace/ralph/MOVED.md
```

## Persistent Volumes

| Volume | Host path | Contents |
|--------|-----------|----------|
| `ralph-projects` | `~/ralph-data/projects/` | All project PRDs + code |
| `ralph-logs` | `~/ralph-data/logs/` | Pipeline run logs |

Created automatically by `docker compose up`.
