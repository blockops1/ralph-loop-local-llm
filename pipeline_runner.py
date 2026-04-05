#!/usr/bin/env python3
"""
pipeline_runner.py — Ralph 3-Stage Pipeline

Stage 1 (CREATE):   Ralph builds the file using PROMPT.md     -> run_story_loop(type=create)
Stage 2 (CRITIQUE): Ralph reviews using PROMPT-critique.md    -> single model call -> critique.md
Stage 3 (FIX):      Ralph fixes using PROMPT-rework.md        -> run_story_loop(type=rework)

Each stage runs in a completely isolated model context.
All three stages use the same model (Qwen3.5-27B) configured in config.yaml.

File lock ensures only one pipeline runs at a time per project.

Usage:
    python3 pipeline_runner.py <project-slug>
    python3 pipeline_runner.py <project-slug> --story US-001
    python3 pipeline_runner.py --list-projects
"""

import argparse
import json
import logging
import os
import re
import sys
import fcntl
import time
import signal
import traceback
import requests
from datetime import datetime, timezone
from pathlib import Path

RALPH_DIR = Path(__file__).parent
sys.path.insert(0, str(RALPH_DIR))

import yaml

from prd_manager import (
    load_prd, save_prd, get_next_story,
    mark_story_done, mark_story_failed,
    append_progress, story_summary,
    get_progress_context,
)
import ralph as ralph_mod


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_cfg() -> dict:
    cfg = {}
    cfg_path = RALPH_DIR / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    return cfg


def model_url_and_id(cfg: dict) -> tuple[str, str]:
    return (
        cfg.get("model_url", "http://localhost:8090/v1"),
        cfg.get("model_id", "Qwen3.5-27B-Q6_K.gguf"),
    )


# ---------------------------------------------------------------------------
# Single model call (raw API — used by CRITIQUE stage)
# ---------------------------------------------------------------------------

def raw_call(
    system_prompt: str,
    user_message: str,
    cfg: dict,
    max_tokens: int = 8192,
) -> str:
    """Call the local model with a single user turn. Returns response text."""
    model_url, model_id = model_url_and_id(cfg)
    resp = requests.post(
        f"{model_url}/chat/completions",
        json={
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        },
        headers={"Content-Type": "application/json"},
        timeout=cfg.get("request_timeout", 14400),
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Stage 1: CREATE
# ---------------------------------------------------------------------------

def stage_create(story: dict, cfg: dict, log: logging.Logger) -> tuple[bool, str]:
    """
    Run Stage 1 CREATE using ralph.py's run_story_loop.
    story['type'] must be 'create' (set by pipeline_for_story).
    """
    log.info(f"[CREATE:{story['id']}] starting (model={cfg.get('model_id')})")
    ok, summary = ralph_mod.run_story_loop(story, cfg, log)
    log.info(f"[CREATE:{story['id']}] done — ok={ok}")
    return ok, summary


# ---------------------------------------------------------------------------
# Stage 2: CRITIQUE
# ---------------------------------------------------------------------------

def _crash_handler(signum, frame):
    """Called if Python itself catches SIGKILL/SIGTERM — rare, but gives us a traceback."""
    sig_name = signal.Signals(signum).name
    tb = "".join(traceback.format_stack(frame))
    # Write to stderr directly since log may be flushed
    sys.stderr.write(f"\n[FATAL] Ralph pipeline received {sig_name} — traceback:\n{tb}\n")
    sys.stderr.flush()
    sys.exit(128 + signum)


# Install SIGTERM handler at import time — SIGKILL is uncatchable on macOS
signal.signal(signal.SIGTERM, _crash_handler)


def stage_critique(
    story: dict,
    cfg: dict,
    log: logging.Logger,
    output_file: str,
) -> tuple[bool, str]:
    """
    Run Stage 2 CRITIQUE.
    Reads the output file, calls the model with PROMPT-critique.md,
    writes critique.md next to the output file.
    Returns (passed, critique_text).
    """
    slug = story.get("_slug", "")
    story_id = story["id"]
    model_url, model_id = model_url_and_id(cfg)
    log.info(f"[CRITIQUE:{story_id}] starting — file={output_file}")

    try:
        # Load PRD (full JSON)
        prd = load_prd(slug) or {}

        # Load output file
        out_path = Path(output_file)
        file_content = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else "(file not found)"

        # Load critique template
        critique_template = (RALPH_DIR / "PROMPT-critique.md").read_text(encoding="utf-8")

        # Build user message: pre-load PRD + file content
        prd_json_text = json.dumps(prd, indent=2)
        user_msg = f"""## PRD (what was supposed to be built)

```json
{prd_json_text}
```

---

## Output File Under Review

**File:** {output_file}

```
{file_content[:40000]}
```

---

## Your Task

{critique_template}

Write your critique to `critique.md` using the write_file tool.
Only write code blocks in your response via write_file — never write code in the response text itself.
"""
        system_prompt = (
            "You are a senior code reviewer. You are shown the PRD (what was supposed to be built) "
            "and the actual output file. Write a critique.md that identifies what must be reworked.\n\n"
            "Your output format:\n"
            "1. First, use write_file tool to write critique.md\n"
            "2. Then call task_complete with a brief summary.\n\n"
            "Do NOT modify the output file. Your job is review only."
        )

        log.info(f"[CRITIQUE:{story_id}] calling model — user_msg_chars={len(user_msg)}")
        try:
            response = raw_call(system_prompt, user_msg, cfg, max_tokens=8192)
        except Exception as e:
            log.error(f"[CRITIQUE:{story_id}] raw_call failed: {e}")
            return False, f"API error: {e}"

        log.info(f"[CRITIQUE:{story_id}] API call done — response_len={len(response)}")

        # Extract and write critique.md
        critique_path = out_path.parent / "critique.md"

        write_match = re.search(
            r'<tool_calls>\s*<tool_call>\s*\{"name":\s*"write_file"', response, re.DOTALL
        )
        if not write_match:
            if "# Critique" in response or "What's Good" in response:
                critique_path.write_text(response, encoding="utf-8")
            else:
                critique_path.write_text(
                    f"# Critique\n\n(Stage 2 model response — may not be complete)\n\n{response}", encoding="utf-8"
                )

        if not critique_path.exists():
            critique_path.write_text(response, encoding="utf-8")

        critique_text = critique_path.read_text(encoding="utf-8", errors="replace")
        log.info(f"[CRITIQUE:{story_id}] written to {critique_path} ({len(critique_text)} chars)")

        has_must_rework = "must rework" in critique_text.lower() or "🔴" in critique_text
        short_critique = len(critique_text.strip()) < 150
        passed = not has_must_rework and not short_critique
        return passed, critique_text

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"[CRITIQUE:{story_id}] Unexpected error: {e}\n{tb}")
        return False, f"Unexpected error: {e}"


# ---------------------------------------------------------------------------
# Stage 3: FIX
# ---------------------------------------------------------------------------

def stage_fix(
    story: dict,
    cfg: dict,
    log: logging.Logger,
    output_file: str,
) -> tuple[bool, str]:
    """
    Run Stage 3 FIX using ralph.py's run_story_loop with a rework story.
    Overwrites the output file with the improved version.
    """
    slug = story.get("_slug", "")
    log.info(f"[FIX:{story['id']}] starting — file={output_file}")

    # Load critique
    critique_path = Path(output_file).parent / "critique.md"
    critique_text = ""
    if critique_path.exists():
        critique_text = critique_path.read_text(encoding="utf-8", errors="replace")

    # Build the rework story for run_story_loop
    # type='rework' causes build_system_prompt to load PROMPT-rework.md
    fix_story = {
        "_slug": slug,
        "id": f"{story['id']}-fix",
        "title": f"Fix issues from critique: {story.get('title', story['id'])}",
        "type": "rework",
        "description": (
            f"Address all 'Must Rework' items from critique.md. "
            f"Read the full critique at: {critique_path}\n\n"
            f"The current implementation is in: {output_file}"
        ),
        "target_file": output_file,
        "output_file": output_file,  # overwrite in-place
        "preserve": [
            "all functional behavior not called out in the critique",
        ],
        "contextFiles": [output_file],
        "acceptanceCriteria": [
            "All 'Must Rework' items from critique.md are addressed",
            "No new bugs or syntax errors introduced",
        ],
        "qualityChecks": story.get("qualityChecks", []),
        # Pass critique text via error field so it appears in the prompt
        "error": f"\n\n## Critique to address:\n{critique_text[:8000]}\n\n",
    }

    ok, summary = ralph_mod.run_story_loop(fix_story, cfg, log)
    log.info(f"[FIX:{story['id']}] done — ok={ok}")
    return ok, summary


# ---------------------------------------------------------------------------
# Full pipeline for one story
# ---------------------------------------------------------------------------

def pipeline_for_story(
    story: dict,
    cfg: dict,
    log: logging.Logger,
) -> tuple[bool, str, str]:
    """
    Run all three stages for one story.
    Returns (overall_ok, summary, output_file_path).
    """
    slug = story.get("_slug", "")
    story_id = story["id"]

    # Primary output file = first contextFile
    context_files = story.get("contextFiles", [])
    output_file = context_files[0] if context_files else f"projects/{slug}/output.txt"

    log.info(f"[PIPELINE:{story_id}] 3-stage pipeline starting | output={output_file}")

    # Stage 1: CREATE
    create_ok, create_summary = stage_create(story, cfg, log)
    if not create_ok:
        append_progress(slug, f"[PIPELINE:{story_id}] CREATE FAILED: {create_summary[:200]}")
        return False, f"CREATE FAILED: {create_summary[:200]}", output_file

    # Stage 2: CRITIQUE
    critique_passed, critique_text = stage_critique(story, cfg, log, output_file)
    has_issues = not critique_passed
    append_progress(
        slug,
        f"[PIPELINE:{story_id}] CRITIQUE: {'PASS — no major issues' if critique_passed else 'FLAGGED issues — running FIX'}"
    )

    # Stage 3: FIX — always runs (validates + improves even if critique is clean)
    fix_ok, fix_summary = stage_fix(story, cfg, log, output_file)

    if not fix_ok:
        append_progress(slug, f"[PIPELINE:{story_id}] FIX FAILED: {fix_summary[:200]}")
        return False, f"CREATE ok, FIX FAILED: {fix_summary[:200]}", output_file

    append_progress(
        slug,
        f"[PIPELINE:{story_id}] ALL 3 STAGES DONE | output={output_file}"
    )
    return True, f"3-stage pipeline complete: {output_file}", output_file


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def acquire_lock(slug: str):
    lock_dir = RALPH_DIR / "projects" / slug
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".pipeline.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"{os.getpid()}\n".encode())
        return fd
    except OSError:
        return None


def release_lock(fd: int):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Notify helper
# ---------------------------------------------------------------------------

def _notify(message: str, log: logging.Logger):
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                m = re.match(r'(?:export\s+)?DISCORD_BOT_TOKEN=(.+)', line)
                if m:
                    bot_token = m.group(1).strip()
                    break
    if not bot_token:
        log.debug("DISCORD_BOT_TOKEN not found — skipping notify")
        return
    try:
        requests.post(
            f"https://discord.com/api/v10/channels/1488012271044132944/messages",
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            json={"content": f"[Ralph] {message}"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Discord notify failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ralph 3-Stage Pipeline Runner")
    parser.add_argument("slug", nargs="?", help="Project slug (e.g. my-project)")
    parser.add_argument("--story", help="Run only this story ID")
    parser.add_argument("--list-projects", action="store_true")
    args = parser.parse_args()

    if args.list_projects:
        projects_dir = RALPH_DIR / "projects"
        for d in sorted(projects_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                prd_file = d / "prd.json"
                status = "has prd.json" if prd_file.exists() else "no prd.json"
                print(f"  {d.name}  [{status}]")
        return

    if not args.slug:
        parser.print_help()
        return

    slug = args.slug.strip().lstrip("/")
    if "/" in slug:
        slug = slug.split("/")[-1]

    cfg = load_cfg()

    # Logging
    log_dir = RALPH_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    log_file = log_dir / f"pipeline-{slug}-{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(str(log_file)),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger(f"ralph-pipeline-{slug}")

    lock_fd = acquire_lock(slug)
    if lock_fd is None:
        log.warning(f"Pipeline already running for '{slug}' — .pipeline.lock exists. Exiting.")
        return

    try:
        log.info(f"=== Ralph 3-Stage Pipeline started | slug={slug} | model={cfg.get('model_id')} | {ts} ===")
        prd = load_prd(slug)
        if prd is None:
            log.error(f"No prd.json found for slug '{slug}'")
            return

        max_iter = cfg.get("max_iterations", 20)
        iteration = 0
        while iteration < max_iter:
            # Pick story
            if args.story:
                target = next(
                    (s for s in prd.get("userStories", []) if s["id"] == args.story),
                    None,
                )
                if target is None:
                    log.error(f"Story '{args.story}' not found")
                    break
                stories_to_run = [target]
            else:
                story = get_next_story(prd)
                if story is None:
                    log.info("No more pending stories — done.")
                    break
                stories_to_run = [story]

            for story in stories_to_run:
                story["_slug"] = slug
                ok, summary, output_file = pipeline_for_story(story, cfg, log)

                if ok:
                    prd = mark_story_done(prd, story["id"], summary)
                    save_prd(prd, slug)
                    append_progress(slug, f"✅ {story['id']} PIPELINE DONE: {summary}")
                    log.info(f"✅ {story['id']} done: {summary}")
                    _notify(f"✅ {story['id']} done (3-stage) in '{slug}' → {output_file}", log)
                else:
                    prd = mark_story_failed(prd, story["id"], summary)
                    save_prd(prd, slug)
                    append_progress(slug, f"❌ {story['id']} PIPELINE FAILED: {summary[:200]}")
                    log.error(f"❌ {story['id']} failed: {summary[:200]}")
                    _notify(f"❌ {story['id']} FAILED in '{slug}': {summary[:120]}", log)

            if args.story:
                break
            iteration += 1

        log.info(f"=== Pipeline finished for '{slug}' ===")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
