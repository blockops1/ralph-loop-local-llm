# Story Writing Rules for Ralph PRDs

Sources: aihero.dev/tips-for-ai-coding-with-ralph-wiggum, medium.com/@haberlah/how-to-write-prds-for-ai-coding-agents, github.com/github/spec-kit

---

## The Fundamental Rule

**The spec is a programming interface, not a human document.** Traditional PRDs are written for engineers to interpret. Ralph needs specs precise enough to execute without interpretation. Every ambiguity in your PRD becomes a wrong decision by Ralph.

---

## Rule 1: Document Current State First

Every story description must start with current state:

✅ Good:
```
Current state: scripts/scanner.py already exists (written 2026-03-05).
It has functions: get_price_ohlcv, get_sm_netflow, get_sm_dex_trades.
Missing: get_top_holders, get_pool_liquidity, MOCK_MODE flag.

What to add: ...
```

❌ Bad:
```
Create scanner.py with the following functions...
```

If Ralph doesn't know the file exists, it will either ignore it or spend 20 tool calls discovering it.

---

## Rule 2: One File Per Story (Usually)

Keep stories atomic. If a story touches more than 2 files, split it.

✅ Good scope:
- "Create config/tokens.json and config/settings.json"
- "Write scripts/support_resistance.py and its test file"

❌ Too large:
- "Build the entire signal pipeline"
- "Create scanner.py, signal_filter.py, and tests for both"

Smaller stories = cleaner context = fewer tool calls = fewer failures.

---

## Rule 3: Risky Tasks First

Order stories so the hardest, most uncertain work comes first. Don't build a polished frontend on an untested API integration.

Ask: "If this story fails, does it make the next 3 stories pointless?" → Move it earlier.

Common priority order:
1. Directory structure / config files (foundations)
2. Integration points (API calls, external services) — highest risk
3. Core logic (pure functions, algorithms)
4. Tests for core logic
5. Polish, formatting, edge cases

---

## Rule 4: Acceptance Criteria Must Be Shell Commands

Every criterion must be verifiable by running a command that exits 0 on pass.

✅ Good:
```json
"acceptanceCriteria": [
  "python3 -m py_compile businesses/base-trader/scripts/scanner.py",
  "NANSEN_MOCK=1 python3 businesses/base-trader/tests/test_scanner.py"
]
```

❌ Bad:
```json
"acceptanceCriteria": [
  "scanner.py should handle errors gracefully",
  "mock mode should work when API key is not set"
]
```

Prose criteria can't be verified. Ralph will mark stories done when they're not.

---

## Rule 5: Be Explicit About What NOT to Do

Ralph will do extra work if you don't constrain it.

Add a "Constraints" section to every description:
```
Constraints:
- Do NOT modify support_resistance.py
- Do NOT add any live API calls — mock mode only
- Do NOT create decision.py — that is a separate story
```

---

## Rule 6: contextFiles — Every Time

If a file exists that Ralph needs to understand before working, put it in `contextFiles`. Ralph reads these before starting.

```json
"contextFiles": [
  "businesses/horizen-zkverify/base-trader/PROJ-base-trader.md",
  "businesses/horizen-zkverify/base-trader/scripts/scanner.py",
  "businesses/horizen-zkverify/base-trader/config/tokens.json"
]
```

**Include existing files Ralph will modify** — if Ralph hasn't read the file, it will overwrite instead of extend.

---

## Rule 7: Quality Checks Are Mandatory

Every story needs at least one `qualityChecks` command. This is what Ralph runs before committing.

```json
"qualityChecks": [
  "python3 -m py_compile {file}",
  "python3 businesses/base-trader/tests/test_scanner.py"
]
```

Use `{file}` as a placeholder for the primary file being written.

---

## Rule 8: Mark Pre-Done Stories Immediately

If work was done before Ralph ran (previous session, manual commit, etc.), don't make Ralph redo it:

```json
{
  "id": "US-001",
  "passes": true,
  "attempts": 0,
  "notes": "Already implemented in commit e2d9883. Verified: all directories exist, tokens.json valid."
}
```

Ralph skips stories where `passes: true`. This saves tokens and prevents rewrites.

---

## Rule 9: Dependencies Must Be Correct

`dependsOn` is enforced — Ralph will skip a story if its dependency failed. Make sure the order is right.

```json
"dependsOn": ["US-001"],
"dependencyPolicy": "block"
```

Common mistake: story 3 depends on story 2, but story 2 was accidentally given higher priority number than story 3. Always verify priority ordering matches dependency chain.

---

## Rule 10: Size Budget Per Story

| Story component | Limit |
|-----------------|-------|
| New code written | ~200 lines max |
| Files touched | 1–2 |
| Test file | 1 (same story as the file it tests) |
| Tool calls expected | < 20 |

If a story would produce >200 lines of new code, split it. Large stories burn context, hit tool call limits, and fail more often.

---

---

## Rule 11: Modification Stories Need Precise Specs + contextFiles — Not Embedded Code

**The single biggest cause of Ralph read-loops:** description says intent ("add pagination"), the file being modified is not in `contextFiles`, so Ralph reads it once to understand it, still doesn't know where to start, reads it again, loop detected.

**Do NOT write the code for Ralph and paste it into the description.** That makes Ralph a copy-paste machine and wastes the purpose of having it. Ralph's job is to write the code. Your job is to give it a spec precise enough to do so.

**The two-part fix:**

**Part 1 — Always put the modified file in `contextFiles`.**
Ralph pre-loads these before starting the story. If the file is there, Ralph doesn't need to discover it mid-run.

```json
"contextFiles": [
  "businesses/horizen-zkverify/base-trader/scripts/scanner.py"
]
```

**Part 2 — Describe the change structurally: what function, what pattern, what API fields, what to preserve.**

✅ Good (precise spec, no embedded code):
```
Current state: _discover_from_holdings() in scripts/scanner.py (line ~368) 
makes a single POST to smart-money/holdings with no pagination. 
Returns ~10 tokens (Nansen hard-caps at 10 per page regardless of limit).

Change needed: wrap the single POST call in a for-loop over pages 1–15.
- Add page param to request body: {'chains': [chain], 'pagination': {'limit': 10, 'page': page}}
- Break if response body has is_last_page=True OR data list is empty
- Keep the existing per-item token-building logic inside the loop unchanged
- Do NOT modify _discover_from_screener() or _discover_from_dex_trades()
```

❌ Bad (intent, not spec):
```
Add pagination to _discover_from_holdings() in scanner.py.
Use page parameter and is_last_page to stop.
Max 15 pages.
```

❌ Also bad (embedded code = Ralph as copy-paste):
```
Replace _discover_from_holdings() with this exact code: [full function here]
```

**The test:** could a competent developer implement this correctly without reading the existing file? If yes, the spec is precise enough. The key ingredients:
- Which function (exact name)
- What the current structure is (single call / loop / etc.)
- What pattern to apply (for-loop, while loop, recursion)
- Which specific API fields / parameters to use
- What to preserve unchanged
- What NOT to touch

---

## Rule 12: Verify Every Acceptance Criterion Before Finalizing

Run every AC command in a terminal before saving prd.json. Criteria that fail before Ralph starts will always fail.

Check for:
- **Truncation:** `p` is not a command. `python3 -m py_compile path/to/file.py` is.
- **Wrong cwd:** `grep 'foo' scripts/scanner.py` fails from `/` but works from workspace root. Decide: always workspace root, or use absolute paths.
- **Grep patterns that won't match:** if Ralph writes `page_num` but AC greps for `page`, it fails. Match the exact identifier Ralph will use.
- **Commands that only verify the easy part:** `grep 'pagination' scanner.py` passes even if the logic is broken. Prefer `python3 tests/test_scanner.py` when tests exist.

**The rule: run every AC command yourself, on the actual file, before launching Ralph.**

---

## Rule 13: Test-Driven Development — Test Story First

For any file with non-trivial logic, write TWO stories:

**Story A: The test file** (runs first)
**Story B: The implementation** (depends on A)

```
US-001  "Write tests/test_scanner.py"
  priority: 1
  dependsOn: []
  target_file: "tests/test_scanner.py"
  output_file: "tests/test_scanner.py"
  contextFiles: ["scripts/scanner.py"]
  description: |
    Current state: tests/test_scanner.py does not exist.
    Create the test file with pytest.
    Use MOCK_MODE and patch network calls so tests run without API keys.
    Test: get_price_ohlcv returns dict with keys open/high/low/close/volume.
    Test: get_sm_netflow returns dict with keys inflow/outflow/net.
    Test: error handling when API returns non-200.
  acceptanceCriteria:
    - python3 -m py_compile tests/test_scanner.py
    - MOCK_MODE=1 python3 -m pytest tests/test_scanner.py -v

US-002  "Build scripts/scanner.py"
  priority: 2
  dependsOn: ["US-001"]
  target_file: "scripts/scanner.py"
  output_file: "scripts/scanner.py"
  contextFiles: ["scripts/scanner.py"]
  description: |
    Current state: scripts/scanner.py does not exist.
    Build the scanner module. All functions must pass the tests in tests/test_scanner.py.
    Use MOCK_MODE for all external API calls.
  acceptanceCriteria:
    - python3 -m py_compile scripts/scanner.py
    - MOCK_MODE=1 python3 -m pytest tests/test_scanner.py -v
```

**Why split them:**
- Story A defines "done" before Story B starts. Ralph implements to the test, not to a prose description.
- The test file survives beyond the implementation — it's regression coverage.
- Splitting forces clear thinking: if you can't write the test, you don't understand the spec.

**What goes in the test story:**
- The test file path and framework (pytest)
- What each test function should verify (function exists, return shape, error handling)
- How mocks/patches work (MOCK_MODE flag, environment variables)
- The AC must run the tests and pass

**What goes in the implementation story:**
- Points to the test file via `dependsOn`
- The implementation must make the tests pass
- No need to restate what the functions do — the tests already defined it

**Constraints:**
- Do NOT write the implementation in the test story
- Do NOT write the test in the implementation story
- The test story should produce a file that compiles and fails (not crashes) before the implementation exists
- After both stories run: `MOCK_MODE=1 python3 -m pytest tests/test_X.py -v` must pass

---

## The INVEST Framework (from Agile, still applies)

Good stories are:
- **I**ndependent — can be implemented without depending on stories not listed in `dependsOn`
- **N**egotiable — implementation approach is open, acceptance criteria are fixed
- **V**aluable — produces something testable and usable on its own
- **E**stimable — scope is clear enough to predict tool call count
- **S**mall — fits in < 20 model calls
- **T**estable — has shell-runnable acceptance criteria
