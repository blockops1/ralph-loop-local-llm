---
name: ralph-prd
description: "Write and validate Product Requirements Documents (PRDs) for the Ralph autonomous coding loop. Ralph has two execution modes: (1) single-stage via ralph.sh — quick iterations, (2) 3-stage pipeline via pipeline_runner.py — production quality. Use when: creating a prd.json for Ralph, planning a new coding project, converting a plan into Ralph format, fixing a failed story's PRD. Trigger even if the user says 'plan this for ralph', 'write the PRD', 'create prd.json', 'prepare Ralph stories', 'set up a ralph project'. Never skip this skill when writing or editing a prd.json — a bad PRD is the #1 cause of Ralph failures."
tags: ["ralph", "prd", "autonomous", "coding", "planning"]
related_skills: ["ralph-loop", "local-llm-manager"]
---

# Ralph PRD Skill

> **Workspace path:** `~/ralph/projects/{slug}/prd.json`
> **NOT** `~/.openclaw/workspace/ralph/projects/` — that directory is deprecated/stale.

Write PRDs that Ralph can actually execute. A bad PRD wastes hours of model time.

## Ralph Has Two Execution Modes

| Mode | Command | Best for |
|------|---------|----------|
| **Single-stage** | `./ralph.sh {slug}` | Quick file fixes, prototyping, one-off stories |
| **3-stage pipeline** | `python3 pipeline_runner.py {slug}` | New projects, anything that matters |

Both modes read the same `prd.json`. The 3-stage pipeline adds a `type` field requirement (see schema below).

**Recommendation:** Write the PRD assuming the 3-stage pipeline. It produces better output. Use single-stage only for quick fixes.

---

## Phase 1: Research (always first)

Before writing any story:

1. **What files already exist?** `ls` the target directory. Any existing file = must be in `contextFiles` or marked `passes: true`.
2. **What does git log say?** `git log --oneline -10` — don't recreate work already done.
3. **What do existing files say?** Read the target files. Ralph reads these too — inconsistency between PRD and reality causes drift.

> **Rule: Never write a story about a file without first checking if it exists.**

---

## Phase 2: Write the Stories

Follow `references/story-rules.md`. Key rules:

- **One file per story** — if it touches 3 files, split it
- **TDD for non-trivial logic: test story first, then implementation** — see Rule 13 in `references/story-rules.md`. For any module with real logic, write the test story (US-00X) before the implementation story (US-00X+1) with a `dependsOn` chain.
- **Risky/unknown first** — don't build on an unproven foundation
- **Current state always documented** — every description answers: what exists now, what changes
- **Acceptance criteria must be shell commands** — not prose
- **`contextFiles` is mandatory** for any story that modifies an existing file
- **No Unicode/em-dashes in descriptions** — causes `SyntaxError`. Use ASCII hyphens only.
- **Run every AC command yourself before launching** — if it fails in your terminal, it fails for Ralph

---

## Phase 3: Validate Before Running

- [ ] Every story that modifies an existing file has that file in `contextFiles`
- [ ] Every AC is a literal shell command (grep, `python3 -m py_compile`, etc.)
- [ ] No em-dashes or Unicode in any text field
- [ ] Every story has `qualityChecks`
- [ ] Risky/integration stories come before polish in priority order
- [ ] **TDD stories**: test story (US-00X) has `priority` one lower than its implementation (US-00X+1), and implementation story has `dependsOn: ["US-00X"]`
- [ ] **TDD stories**: test story AC runs the test and passes (even if implementation doesn't exist yet — the test should fail gracefully, not crash)
- [ ] PYTHONPATH: ACs running `python3 tests/test_X.py` — does the test use `from module import`? If yes, set `PYTHONPATH=scripts` in the command or the test needs its own sys.path
- [ ] Run every AC manually in your terminal

---

## prd.json Schema

**Root:** `{"userStories": [...]}` — **never `"stories"`** (ralph.py L572 silently skips).

```json
{
  "project": "my-project",
  "slug": "my-project",
  "branchName": "ralph/my-project-feature",
  "description": "What this PRD builds and why.",
  "contextFiles": ["PROJ-my-project.md"],
  "qualityChecks": ["python3 -m py_compile {file}"],
  "userStories": [
    {
      "id": "US-001",
      "title": "One-line title — include filename if creating",
      "type": "create",
      "description": "Current state: ...\n\nWhat to build: ...\n\nConstraints: ...",
      "target_file": "path/to/output.py",
      "output_file": "path/to/output.py",
      "preserve": ["behavior not to change"],
      "acceptanceCriteria": [
        "python3 -m py_compile path/to/output.py",
        "python3 tests/test_output.py"
      ],
      "qualityChecks": ["python3 -m py_compile {file}"],
      "priority": 1,
      "passes": false,
      "attempts": 0,
      "dependsOn": [],
      "dependencyPolicy": "block",
      "contextFiles": ["path/to/existing_file.py"]
    }
  ]
}
```

### Story `type` Field

| type | Meaning | Used by |
|------|---------|---------|
| `"create"` | Build a new file or rewrite an existing one from scratch | Single-stage + 3-stage pipeline |
| `"review"` | Code review (single-stage only) | Single-stage Ralph |
| `"rework"` | Improve/fix an existing file (single-stage only) | Single-stage Ralph |

**For 3-stage pipeline:** only `type: "create"` stories are supported. The pipeline's FIX stage handles reworks automatically.

### Pipeline-Specific Fields (3-stage)

When using `pipeline_runner.py`, these additional fields are required:

| Field | Required | Notes |
|-------|----------|-------|
| `type` | Yes | Must be `"create"` for pipeline stories |
| `target_file` | Yes | Path Ralph will write to (usually same as `output_file`) |
| `output_file` | Yes | Same as `target_file` for create stories |
| `preserve` | Recommended | Behaviors to keep unchanged (array of strings) |
| `contextFiles` | Yes | Primary output file should be `contextFiles[0]` |

### Standard Fields (both modes)

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Format: `US-001`, `REV-A`, etc. |
| `title` | Yes | One line with filename |
| `description` | Yes | `Current state:` section mandatory |
| `acceptanceCriteria` | Yes | Shell commands only — exit 0 = pass |
| `qualityChecks` | Yes | At least one command |
| `priority` | Yes | Lower = runs first |
| `passes` | Yes | `false` initially; `true` if already done |
| `attempts` | Yes | Start at `0` |
| `dependsOn` | Yes | `[]` if none |
| `dependencyPolicy` | Yes | Always `"block"` |

---

## CRITIQUE Stage (3-Stage Pipeline)

The 3-stage pipeline's CRITIQUE stage (`stage_critique` in `pipeline_runner.py`) is a **single model call** that reads the entire output file and the full PRD JSON. This creates a timeout risk on the Hermes gateway, which has a **10-minute agent command timeout**.

### CRITIQUE Timeout Symptoms

- CRITIQUE never completes: the model takes too long to read + analyze + generate
- File is large (e.g., 990+ lines) with large context (~15,500 tokens)
- Gateway kills the agent command before CRITIQUE finishes

### How to Prevent CRITIQUE Timeouts

**Rule: Keep output files small enough that CRITIQUE can read them in context.**

1. **Split large stories** so CRITIQUE only reads the modified portion, not the whole file. If a file exceeds ~400 lines, consider splitting the work into two stories.

2. **Keep contextFiles minimal** — CRITIQUE already loads the full PRD JSON. Do not add unnecessary files to `contextFiles` for a story that will go through the 3-stage pipeline.

3. **Use targeted prompts** — describe only what changed in the story description, not the entire file history.

4. **Prefer single-stage** (`ralph.sh`) for large file rewrites — the 3-stage CRITIQUE/FIX cycle is designed for modular, incremental work.

### git_commit Failures Are Non-Fatal

The `git_commit` tool calls `git status --porcelain` internally, which may trigger `'nc is not allowed'` errors in some environments. This is **non-fatal** — the story is already complete.

**When `git_commit` fails repeatedly, call `task_complete` anyway rather than retrying.** Do not let a git commit failure block story completion. The work is saved to disk regardless.

---

## Common Failures and Fixes

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Ralph loops reading same file 3x | File not in `contextFiles` + intent-only description | Put file in `contextFiles`. Write structural spec: which function, what pattern, what to preserve. |
| Ralph rewrites a file already correct | No "current state" in description | Add "File already exists — review before modifying" |
| Story passes but logic wrong | AC was prose, not executable | Replace with `python3 tests/test_X.py` |
| Story fails every attempt | Scope too large | Split into 2–3 smaller stories |
| AC fails with `command not found: p` | `qualityChecks` was a string and got iterated char-by-char | Must be a list `["command"]` |
| write_file rejected: truncation | Model truncated internally, wrote `... [truncated]` | `tools.py` catches this; Ralph re-reads and rewrites |
| Story hits max_attempts, goes silent | Was: silent failure | Now: BLOCKED status + notification. Reset `passes: false, attempts: 0` |
| 500 OOM from llama-server | Context too large during generation | Keep one file per story; keep files < 200 lines |
| CRITIQUE stage times out | File too large + large PRD context; exceeds 10-min gateway timeout | Split into smaller stories; keep files < 400 lines; use single-stage for large rewrites |
| git_commit fails with 'nc not allowed' | `git status --porcelain` blocked in some environments | Non-fatal — call `task_complete` anyway; work is on disk |

---

## Production Systems: Branch Policy

**Is the target system live?** (cron job runs against real money/APIs without human approval per run)

If YES — Ralph must NOT write to `main`. The PRD must:
1. Start with `git checkout dev` (or `ralph/<slug>` branch)
2. Target all paths on the dev branch
3. Include a final story for test gate and merge procedure

If NO (greenfield/sandbox): target files directly.

**When in doubt, ask Mr. V.**

---

## Sequential Execution Rule

Never launch multiple `ralph.sh` or `pipeline_runner.py` calls simultaneously on the same project. llama-server is single-threaded — parallel runs deadlock.

For chains of PRDs:
```bash
./ralph.sh slug-1
./ralph.sh slug-2
```

For 3-stage pipeline — `pipeline_runner.py` has its own lockfile per project:
```bash
python3 pipeline_runner.py slug-1
python3 pipeline_runner.py slug-2
```

---

## References

- `references/story-rules.md` — Full ruleset for writing stories
- `references/schema.md` — Complete field reference with examples
