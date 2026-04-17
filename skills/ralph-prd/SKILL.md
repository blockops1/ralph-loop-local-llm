---
name: ralph-prd
description: "Write and validate Product Requirements Documents (PRDs) for the Ralph autonomous coding loop. Ralph runs as a 3-stage pipeline (CREATE → CRITIQUE → FIX) inside a Docker container. Use when: creating a prd.json for Ralph, planning a new coding project, converting a plan into Ralph format, fixing a failed story's PRD. Trigger even if the user says 'plan this for ralph', 'write the PRD', 'create prd.json', 'prepare Ralph stories', 'set up a ralph project'. Never skip this skill when writing or editing a prd.json — a bad PRD wastes hours of model time."
tags: ["ralph", "prd", "autonomous", "coding", "planning"]
related_skills: ["ralph-loop"]
---

# Ralph PRD Skill

Write PRDs that Ralph can actually execute. A bad PRD wastes hours of model time.

## Ralph Execution Mode

Ralph runs inside Docker as a **3-stage pipeline** (CREATE → CRITIQUE → FIX). Launch:

| What | Command |
|------|---------|
| All stories | `docker compose run --rm ralph {slug}` |
| Single story | `docker compose run --rm ralph {slug} --story US-001` |
| List projects | `docker compose run --rm ralph --list-projects` |

---

## Phase 1: Research (always first)

Before writing any story:

1. **What files already exist?** `ls` the target directory. Any existing file = must be in `contextFiles` or marked `passes: true`.
2. **Estimate how many stories you need.** One story per function, or one story per logical piece of work. If the whole project feels like 10+ stories, write them all.
3. **Check git log** — don't recreate work already done.
4. **Read existing files** — inconsistency between PRD and reality causes drift.

> **Rule: Never write a story about a file without first checking if it exists.**

---

## Phase 2: Write the Stories

Follow `skills/ralph-prd/references/story-rules.md`. Key rules:

- **Hard limit: ~100 lines of new code per story (150 max)** — the #1 failure cause is over-scoped stories
- **One file per story** — if it touches 3 files, split it
- **More stories is always safer** — Ralph handles many small stories better than one large one
- **TDD for non-trivial logic:** test story first (lower priority), then implementation with `dependsOn`
- **Risky/unknown first** — don't build on an unproven foundation
- **`contextFiles` is mandatory** for any story that modifies an existing file
- **No Unicode/em-dashes** — use ASCII hyphens only
- **Run every AC command yourself before launching** — if it fails in your terminal, it fails for Ralph

---

## Phase 3: Validate Before Running

- [ ] **Hard size check: no story should exceed ~150 lines of new code.** Split first if needed.
- [ ] Every story that modifies an existing file has that file in `contextFiles`
- [ ] Every AC is a literal shell command
- [ ] No em-dashes or Unicode in any text field
- [ ] Every story has `qualityChecks`
- [ ] Risky/integration stories come before polish in priority order
- [ ] **TDD stories:** test story has `priority` one lower than implementation, and implementation has `dependsOn: ["US-00X"]`
- [ ] **TDD stories:** test AC runs the test and passes gracefully (no crash) even before implementation exists
- [ ] Run every AC manually in your terminal first

---

## PRD Schema

**Root:** `{"userStories": [...]}` — **never `"stories"`** (pipeline reads this field).

Full schema in `skills/ralph-prd/references/schema.md`.

### Story Fields at a Glance

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Format: `US-001`, `US-002`, etc. |
| `title` | Yes | One line — include filename if creating |
| `type` | Yes | Must be `"create"` for 3-stage pipeline |
| `description` | Yes | Must include `Current state:` section |
| `target_file` | Yes | Container path (e.g. `/app/projects/slug/code/file.py`) |
| `output_file` | Yes | Same as `target_file` for create stories |
| `preserve` | Recommended | What not to break |
| `contextFiles` | Yes | Existing files must be here |
| `acceptanceCriteria` | Yes | Shell commands — exit 0 = pass |
| `qualityChecks` | Yes | At least one command |
| `priority` | Yes | Lower = runs first |
| `passes` | Yes | Start as `false` |
| `attempts` | Yes | Start at `0` |
| `dependsOn` | Yes | `[]` if none |
| `dependencyPolicy` | Yes | Always `"block"` |

---

## CRITIQUE Stage — Timeout Prevention

CRITIQUE is a single model call that reads the full output file + full PRD JSON. Large files + large context = timeout risk.

**Prevent timeouts:**
- Keep output files under ~400 lines
- Keep `contextFiles` minimal for 3-stage pipeline stories
- Split large stories before they become unmanageable

---

## Common Failures

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| Ralph loops reading same file | File not in `contextFiles` | Add file + structural spec to `contextFiles` |
| Ralph rewrites correct file | No "current state" in description | Add "File already exists — review before modifying" |
| Story passes but logic wrong | AC was prose, not executable | Replace with `python3 tests/test_X.py` |
| Story fails every attempt | Scope too large | Split into 2–3 smaller stories |
| AC fails with `command not found: p` | `qualityChecks` was a string, not a list | Must be `["command"]`, not `"command"` |
| CRITIQUE times out | File too large | Keep files under 400 lines; split story |

---

## References

- `skills/ralph-prd/references/story-rules.md` — 16 rules for writing stories
- `skills/ralph-prd/references/schema.md` — complete field reference with examples
