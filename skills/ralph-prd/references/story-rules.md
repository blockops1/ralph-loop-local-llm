# Story Writing Rules for Ralph

These rules come from hard-won experience. Breaking them is the #1 cause of pipeline failures.

---

## Rule 1: One Story = One File = One Concept

A story should produce exactly one file, and that file should do one thing.

**Wrong:** "Create a config parser that handles INI files AND writes them back AND validates schema"
**Right:** Split into three stories — INI reader, INI writer, schema validator

---

## Rule 2: Hard Cap — ~100 Lines of New Code Per Story (150 Max)

This is the single most common failure mode. If a story will produce more than 150 lines, split it.

**Signs a story is too large:**
- Description exceeds ~150 lines
- Contains "also..." or "while we're at it..."
- Multiple `target_file` or `output_file` entries
- Acceptance criteria list is long or compound

**When in doubt: split.** Five 50-line stories beat one 250-line story every time.

---

## Rule 3: More Stories Is Always Safer

Ralph is faster at running many small stories than failing at one large one. When you're unsure whether to split, split.

A good test: can you describe what this story does in one sentence? If not, it might be too large.

---

## Rule 4: TDD for Non-Trivial Logic

For any module with real logic, write the test story first, then the implementation.

1. Story US-00X (priority 1): Write the test file
2. Story US-00X+1 (priority 2, `dependsOn: ["US-00X"]`): Implement the module

The test story should `passes: false` initially and have an AC that runs the test. The test should fail gracefully (no crash), proving Ralph can run it.

---

## Rule 5: Existing Files Must Be in contextFiles

Any story that modifies an existing file must list that file in `contextFiles`. Ralph needs to see the current state before changing it.

If a file exists and is not in `contextFiles`, Ralph may try to create a new file instead of editing the existing one.

---

## Rule 6: contextFiles[0] Should Be the Primary Output File

The first entry in `contextFiles` should be the file Ralph is creating or modifying. This is a convention that helps the model prioritize.

---

## Rule 7: Current State Is Mandatory

Every `description` must begin with a `Current state:` section that describes what exists before the story runs.

**Template:**
```
Current state: What exists now, what the file looks like, what behavior is present.

What to build: The new behavior, function, or change.

Constraints: What to preserve, what not to change.
```

---

## Rule 8: Acceptance Criteria Must Be Shell Commands

AC entries must be literal shell commands that exit 0 on success.

**Wrong:** "The function should parse the config correctly"
**Right:** `python3 -c "from config import parse; assert parse('key=value') == {'key': 'value'}"`

**Wrong:** `qualityChecks: "python3 -m py_compile {file}"` (string, not list)
**Right:** `qualityChecks: ["python3 -m py_compile {file}"]` (list)

---

## Rule 9: Run Every AC Manually Before Launching

If an AC fails in your terminal, it will fail for Ralph. Test every acceptance criterion yourself first.

---

## Rule 10: Risky/Uncertainty First

Put stories with unknown dependencies or higher risk earlier in the priority order. Don't build on an unproven foundation.

---

## Rule 11: No Em-Dashes or Unicode

Use ASCII hyphens only. Em-dashes and other Unicode characters in descriptions cause `SyntaxError` in Python when Ralph generates code.

---

## Rule 12: Test Your Own ACs for Bugs

If your acceptance criterion has a bug, Ralph will spend hours failing against it.

**Example:** A US-012 failure was caused by this buggy AC:
```python
# WRONG — configparser lowercases dict keys
assert 'Restart=always' in dict(c['Service'])

# CORRECT — compare lowercase keys
assert 'restart=always' in {k.lower(): v for k, v in dict(c['Service']).items()}
```

---

## Rule 13: scope > new_code_lines

The `scope` field (if used) should estimate the conceptual complexity, not just line count. A 50-line story that touches 3 systems is larger than a 100-line story in a sandbox.

---

## Rule 14: git_commit Failures Are Non-Fatal

The `git_commit` tool may fail with `'nc is not allowed'` in some environments. This is non-fatal — the work is on disk. Call `task_complete` rather than retrying indefinitely.

---

## Rule 15: Keep contextFiles Minimal for 4-Stage Pipeline

The CRITIQUE stage (stage 2 of the pipeline) reads the full PRD JSON plus the entire output file in a single model call. Large contextFiles increase the risk of CRITIQUE timing out.

For 4-stage pipeline stories: only include files Ralph will actually modify in `contextFiles`.

---

## Rule 16: Never Write a Story About a File Without Checking If It Exists

Before writing a story, `ls` the target directory. Existing files must be handled as modifications, not creations.
