# Ralph System Fixes

Document tracking all identified issues in the Ralph autonomous coding loop.
Fixes applied 2026-04-05.

---

## Issue 1: `nc` Substring Matching Breaks All Compound Shell Commands

**Severity:** 🔴 Critical  
**File:** `tools.py` lines 337-357  
**Status:** ✅ FIXED 2026-04-05

**Root cause:** `_BLOCKED_COMMANDS` used substring matching. `"nc"` matched inside `NANSEN_MOCK` and `||`, breaking compound shell commands.

**Fixes applied:**
1. **Short blocker word boundaries:** `nc`, `dd`, `rm`, etc. now use `(?<![a-zA-Z0-9_-]){blocked}\b` regex — so `nc` no longer matches inside `NANSEN_MOCK` or `||`.
2. **Env var prefix stripping:** `VAR=value python3 x.py` now strips the env prefix before checking — sees `python3` as first token, not `VAR`.
3. **`vi` in git flags:** `git commit -vi` was being blocked as `vi` (word boundary matches `-vi`). Added `(?<![a-zA-Z0-9_-])vi\b` negative lookbehind so `-vi` flag passes.

**Verification:**
```
OK | compound OR               | got=False expect=False   (test -f x || echo y)
OK | NANSEN_MOCK env var       | got=False expect=False   (NANSEN_MOCK=1 python3 x)
OK | actual netcat             | got=True  expect=True    (nc -l 8090)
OK | git commit -vi           | got=False expect=False   (-vi flag preserved)
OK | curl URL                  | got=False expect=False   (curl https://...)
OK | actual rm/dd/launchctl    | got=True  expect=True
```

---

## Issue 2: 500 Errors From Llama-Server Kill Stories

**Severity:** 🔴 Critical  
**File:** `ralph.py` line 537  
**Status:** ✅ FIXED 2026-04-05

**Root cause:** 500 Internal Server Error from llama-server (caused by Qwen's malformed tool call JSON) was not in the retry list. Story failed immediately.

**Fix applied:** Added `"500"` to retryable error codes. Now retries up to 3 times on 500, 502, 503, 429. Also widened log from 60 → 80 chars.

---

## Issue 3: `curl`/`wget` Blocked Despite Being in Allowlist

**Severity:** 🟠 Medium  
**File:** `tools.py` lines 274-277  
**Status:** ✅ FIXED 2026-04-05

**Root cause:** `curl` and `wget` were in BOTH `_BLOCKED_COMMANDS` and `_ALLOWED_COMMANDS`. Blocklist checked first → always blocked. Allowlist entry was dead code.

**Fix applied:** Removed `curl` and `wget` from `_BLOCKED_COMMANDS`. They remain in `_ALLOWED_COMMANDS`.

---

## Issue 4: Sanitization Destroys Unicode in Python Source

**Severity:** 🟠 Medium  
**File:** `tools.py` lines 192-211, 444-452  
**Status:** ✅ FIXED 2026-04-05

**Root cause:** Two problems:
1. `write_file`: `_sanitize_for_python` had a catch-all that replaced ALL non-ASCII (ord > 127) with `_`. Destroyed Unicode in string literals, identifiers, comments.
2. `git_commit`: Stripped ALL non-ASCII (ord >= 127), not just control chars. Commit messages lost accented letters, emoji.

**Fix applied:**
- `write_file`: Removed the catch-all. Now only normalizes the `_UNICODE_SUBS` table (smart quotes → ASCII, em-dashes → `-`, non-breaking spaces → ` `). Python 3 passes UTF-8 natively — Unicode in source is preserved.
- `git_commit`: Changed to strip only C0/C1 control characters (`[\x00-\x08\x0b\x0c\x0e-\x1f]`). Preserves accented letters, emoji, CJK in commit messages.

**Verification:**
```
Smart quotes → normalized:     ✓ (em-dash, smart quotes replaced)
Unicode in strings preserved:   ✓ ('café', 'naïve', '🎉' all pass through)
Non-.py files untouched:        ✓ (no substitution on .txt/.md/etc.)
```

---

## Issue 5: Stale Default Port 8091

**Severity:** 🟡 Low  
**File:** `pipeline_runner.py` line 63  
**Status:** ✅ FIXED 2026-04-05

**Fix applied:** Changed default from `http://localhost:8091/v1` → `http://localhost:8090/v1`.

---

## Issue 6: CRITIQUE Prompt Says `git diff` But Receives File Content

**Severity:** 🟡 Low  
**File:** `PROMPT-critique.md` lines 19-24  
**Status:** ✅ FIXED 2026-04-05

**Fix applied:** Updated prompt to say "The completed source file(s)" instead of "git diff HEAD".

---

## Issue 7: PROMPT-rework.md Inconsistent Format With PROMPT.md

**Severity:** 🟡 Low  
**File:** `PROMPT-rework.md` lines 11-29  
**Status:** ✅ FIXED 2026-04-05

**Root cause:** Rework prompt required `<thinking>`, `<plan>`, `<tool_calls>`, `<final_summary>` XML wrappers. CREATE prompt said "no planning text, first response must be a tool call". Inconsistent formatting could cause parse failures.

**Fix applied:** Rework prompt now reads "Your response must be a direct tool_call. No planning text, no markdown, no explanations before or after." Matches CREATE prompt.

---

## Files Modified

| File | Change |
|------|--------|
| `tools.py` | Blocking logic (word boundaries, env prefix strip, vi edge case), sanitization (preserve Unicode), curl/wget unblocked |
| `ralph.py` | 500 added to retryable errors |
| `pipeline_runner.py` | Default port 8091 → 8090 |
| `PROMPT-critique.md` | "git diff" → "source file(s)" |
| `PROMPT-rework.md` | XML wrapper format removed |
| `RALPH-FIXES.md` | This document |
