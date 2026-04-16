---
name: ralph-prd
description: "Write and validate Product Requirements Documents (PRDs) for the Ralph autonomous coding loop. Use when: creating a prd.json for Ralph, planning a new coding project for Ralph, converting a plan or feature spec into Ralph format, or reviewing/fixing an existing prd.json before running Ralph. Trigger even if the user says 'plan this for ralph', 'write the PRD', 'create prd.json', 'prepare Ralph stories', 'set up a ralph project', or 'why did ralph fail on this story'. Never skip this skill when writing or editing a prd.json — a bad PRD is the #1 cause of Ralph failures."
---

# Ralph PRD Skill

**Core principle: Simple stories, simple failures.** Ralph succeeds when each story does one thing. Ralph fails when a story tries to do five things at once — it runs out of tool calls, context gets corrupted, and the PRD has to be rewritten.

**Rules of thumb:**
- Max ~150 lines of new code per story
- 1–2 files touched per story
- One logical concern per story (split model loading from model usage, split single-collection from cross-collection)
- Max 20 tool calls expected per story
- Acceptance criteria must be shell commands that exit 0
- Every story needs qualityChecks

Write PRDs that Ralph can actually execute. A bad PRD wastes hours of model time.
The spec is the source of truth — if Ralph fails, the PRD is usually the cause.

## Anti-Loop
- Max 6 tool steps per turn. Stop and summarize if looping.
- Max 2 retries on any action → explain and ask for direction.

---

## The Three Phases

### Phase 1: Research (always do this first)

Before writing a single story, answer these questions from the workspace:

1. **What files already exist?** Run `list_dir` on the target directory. Any file that already exists must be documented in the story — or marked `"passes": true` if it already satisfies acceptance criteria.
2. **What does the git log say?** Run `git log --oneline -10` to see what was recently committed. Don't ask Ralph to recreate work that's already done.
3. **What do the context files say?** Read the PROJ doc and any strategy files the stories will reference. Ralph reads these too — inconsistency between the PRD and the PROJ doc causes drift.

> Rule: **Never write a story about a file without first checking if it exists.**

### Phase 2: Write the Stories

Follow the rules in `references/story-rules.md`. Key principles:

- **One file per story** — if a story touches 3 files, split it
- **Risky/unknown first** — don't build on an unproven foundation
- **Current state always documented** — every story description answers: what exists now, what needs to change
- **Acceptance criteria must be shell-runnable** — prose is not verifiable
- **Quality checks must be concrete commands** — `python3 -m py_compile {file}`, `python3 tests/test_X.py`

### Phase 3: Validate Before Running

Run through this checklist before enabling the cron:

- [ ] Dependencies are in correct order (no story depends on a later story)
- [ ] Every story has at least one shell-runnable acceptance criterion
- [ ] Every story has a `qualityChecks` command
- [ ] Files that already exist are listed in `contextFiles` with a note in the description
- [ ] Files that already fully pass acceptance criteria have `"passes": true, "attempts": 0`
- [ ] No story scope exceeds ~200 lines of new code (if larger, split it)
- [ ] Risky/integration stories come before polish stories in priority order
- [ ] **PYTHONPATH check:** Any acceptance criterion that runs `python3 tests/test_X.py` — does the test file use `from module import X`? If yes, either the test must do its own `sys.path.insert` OR the criterion must set `PYTHONPATH=scripts`. Test it manually before finalizing.
- [ ] **Script imports check:** Any script that imports sibling scripts (e.g., run_pipeline.py importing scanner.py) — the description must include `sys.path.insert(0, str(Path(__file__).parent))` so Ralph knows to write it. Without this, the script will fail with `ModuleNotFoundError` when run from outside its own directory.

---

## prd.json Schema (CRITICAL)

**Root:** `{"userStories": [array-of-stories]}` — **"stories" fails silently (ralph.py L572)**.

Minimal valid story:
```json
{
  "id": "US-001",
  "title": "One-line title",
  "description": "Current state: X exists / does not exist.\\n\\nWhat to build: ...\\n\\nConstraints: ...",
  "acceptanceCriteria": [
    "python3 -m py_compile path/to/file.py passes",
    "python3 tests/test_X.py exits 0"
  ],
  "priority": 1,
  "passes": false,
  "attempts": 0,
  "dependsOn": [],
  "dependencyPolicy": "block",
  "contextFiles": ["path/to/existing-file.py"]
}
```

**Lesson 2026-03-12:** `{"stories": [...]}` → 0 stories detected → "All complete" instant exit. Always use `"userStories"`.

See `references/schema.md` for full field reference.

Minimal valid story:
```json
{
  "id": "US-001",
  "title": "One-line title",
  "description": "Current state: X exists / does not exist.\n\nWhat to build: ...\n\nConstraints: ...",
  "acceptanceCriteria": [
    "python3 -m py_compile path/to/file.py passes",
    "python3 tests/test_file.py exits 0"
  ],
  "priority": 1,
  "passes": false,
  "attempts": 0,
  "dependsOn": [],
  "dependencyPolicy": "block",
  "contextFiles": ["path/to/existing-file.py"]
}
```

---

## Common Failures and Fixes

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Ralph spends 20+ tool calls reading files | File existed, not in `contextFiles` | Add existing files to `contextFiles`, note in description |
| Ralph rewrites a file that was already correct | No "current state" in description | Add "File already exists at X — review before modifying" |
| Story passes but logic is wrong | Acceptance criteria was prose, not executable | Replace prose with `python3 tests/test_X.py` |
| Story fails every attempt | Scope too large | Split into 2–3 smaller stories |
| Ralph builds story 3 on broken story 2 output | Wrong dependency order | Set `dependsOn` correctly |
| Ralph does extra work not in the story | Description too vague | Be explicit: "Only create X. Do not modify Y." |
| Story fails with `ModuleNotFoundError` | Script imports siblings without sys.path setup | Add `sys.path.insert(0, str(Path(__file__).parent))` to the description's import block |
| Test fails with `ModuleNotFoundError` | Acceptance criterion runs test without PYTHONPATH | Add `PYTHONPATH=scripts` to the criterion, or verify the test self-manages its path |
| Story marked failed but files are actually correct | Tool call limit hit before acceptance checks ran | Manually run the acceptance criteria — if they pass, mark story done in prd.json |
| Later stories don't run after one story fails | `dependsOn` was ignored (pre-2026-03-12 bug, now fixed) | Verify prd_manager.py has dependency-aware `get_next_story` |

---

## References
- `references/story-rules.md` — Full ruleset for writing good stories
- `references/schema.md` — Complete prd.json field reference with examples
