"""
prd_manager.py - Ralph Loop PRD and progress file management.

Handles reading/writing prd.json, tracking story status,
managing progress.txt, and archiving completed runs.
"""

import json
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ralph.prd")

RALPH_DIR = Path(__file__).parent
PROJECTS_DIR = RALPH_DIR / "projects"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def project_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug

def prd_path(slug: str) -> Path:
    return project_dir(slug) / "prd.json"

def progress_path(slug: str) -> Path:
    return project_dir(slug) / "progress.txt"

def agents_md_path(slug: str) -> Path:
    return project_dir(slug) / "AGENTS.md"

def lock_path(slug: str) -> Path:
    return project_dir(slug) / ".ralph.lock"

def archive_dir(slug: str) -> Path:
    return project_dir(slug) / "archive"


# ---------------------------------------------------------------------------
# PRD load / save
# ---------------------------------------------------------------------------

def load_prd(slug: str) -> dict:
    """Load and return prd.json for the given project slug."""
    path = prd_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"No prd.json found for project '{slug}' at {path}")
    with open(path) as f:
        return json.load(f)


def save_prd(prd: dict, slug: str) -> None:
    """Write prd.json back to disk."""
    path = prd_path(slug)
    with open(path, "w") as f:
        json.dump(prd, f, indent=2)
    log.info(f"Saved prd.json for '{slug}'")


# ---------------------------------------------------------------------------
# Story management
# ---------------------------------------------------------------------------

def get_next_story(prd: dict, max_attempts: int = 3) -> dict | None:
    """
    Return the highest-priority story where:
    - passes=False
    - attempts < max_attempts
    - all dependsOn stories have passes=True

    Returns None if all stories are done or all remaining are blocked/exhausted.
    """
    stories = prd.get("userStories", [])
    passed_ids = {s["id"] for s in stories if s.get("passes", False)}

    def deps_satisfied(story: dict) -> bool:
        return all(dep in passed_ids for dep in story.get("dependsOn", []))

    pending = [
        s for s in stories
        if not s.get("passes", False)
        and s.get("attempts", 0) < max_attempts
        and deps_satisfied(s)
    ]
    if not pending:
        return None
    return min(pending, key=lambda s: s.get("priority", 999))


def all_done(prd: dict) -> bool:
    """Return True if every story has passes=True."""
    return all(s.get("passes", False) for s in prd.get("userStories", []))


def any_blocked(prd: dict, max_attempts: int = 3) -> bool:
    """Return True if any story has hit max attempts without passing."""
    return any(
        not s.get("passes", False) and s.get("attempts", 0) >= max_attempts
        for s in prd.get("userStories", [])
    )


def mark_story_done(prd: dict, story_id: str, notes: str = "") -> dict:
    """Mark a story as passing. Returns updated prd."""
    for story in prd.get("userStories", []):
        if story["id"] == story_id:
            story["passes"] = True
            story["notes"] = notes
            log.info(f"Story {story_id} marked DONE")
            break
    return prd


def mark_story_failed(prd: dict, story_id: str, error: str = "") -> dict:
    """Increment attempt count and record error. Returns updated prd."""
    for story in prd.get("userStories", []):
        if story["id"] == story_id:
            story["attempts"] = story.get("attempts", 0) + 1
            story["lastAttempt"] = datetime.now(timezone.utc).isoformat()
            story["error"] = error[:500] if error else ""
            log.info(f"Story {story_id} attempt {story['attempts']} FAILED: {error[:80]}")
            break
    return prd


def mark_story_blocked(prd: dict, story_id: str, error: str = "") -> dict:
    """Mark a story as permanently blocked (hit max attempts). Returns updated prd."""
    for story in prd.get("userStories", []):
        if story["id"] == story_id:
            story["status"] = "blocked"
            story["blockedAt"] = datetime.now(timezone.utc).isoformat()
            story["blockReason"] = error[:500] if error else "max attempts reached"
            log.warning(f"Story {story_id} marked BLOCKED: {error[:80]}")
            break
    return prd


def get_blocked_stories(prd: dict, max_attempts: int = 3) -> list:
    """Return list of stories that have hit max_attempts without passing."""
    return [
        s for s in prd.get("userStories", [])
        if not s.get("passes", False) and s.get("attempts", 0) >= max_attempts
    ]


def story_summary(prd: dict) -> str:
    """Return a one-line status string: 'X/Y stories complete'."""
    stories = prd.get("userStories", [])
    done = sum(1 for s in stories if s.get("passes", False))
    return f"{done}/{len(stories)} stories complete"


# ---------------------------------------------------------------------------
# Progress log
# ---------------------------------------------------------------------------

def append_progress(slug: str, text: str) -> None:
    """Append a timestamped entry to progress.txt."""
    path = progress_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a") as f:
        f.write(f"\n## [{ts}]\n{text.strip()}\n")
    log.info(f"Appended to progress.txt for '{slug}'")


def get_progress_context(slug: str, max_lines: int = 80) -> str:
    """Read the last max_lines lines of progress.txt. Returns empty string if not found."""
    path = progress_path(slug)
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        return f"[... truncated to last {max_lines} lines ...]\n" + "\n".join(lines)
    return "\n".join(lines)


def init_progress(slug: str) -> None:
    """Create a fresh progress.txt if it doesn't exist."""
    path = progress_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# Ralph Progress Log - {slug}\nStarted: {datetime.now().isoformat()}\n---\n")


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def archive_if_branch_changed(slug: str, prd: dict) -> bool:
    """
    If the branchName in prd.json differs from the last recorded branch,
    archive the current progress.txt and prd.json, then reset progress.txt.
    Returns True if an archive was created.
    """
    last_branch_file = project_dir(slug) / ".last-branch"
    current_branch = prd.get("branchName", "")

    last_branch = ""
    if last_branch_file.exists():
        last_branch = last_branch_file.read_text().strip()

    if current_branch and last_branch and current_branch != last_branch:
        date_str = datetime.now().strftime("%Y-%m-%d")
        folder_name = last_branch.replace("ralph/", "").replace("/", "-")
        arch = archive_dir(slug) / f"{date_str}-{folder_name}"
        arch.mkdir(parents=True, exist_ok=True)

        for fname in ("prd.json", "progress.txt"):
            src = project_dir(slug) / fname
            if src.exists():
                shutil.copy2(src, arch / fname)

        # Reset progress for new run
        progress_path(slug).write_text(
            f"# Ralph Progress Log - {slug}\nStarted: {datetime.now().isoformat()}\n"
            f"Branch: {current_branch}\n---\n"
        )
        log.info(f"Archived previous run to {arch}")
        last_branch_file.write_text(current_branch)
        return True

    if current_branch:
        last_branch_file.write_text(current_branch)
    return False


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def acquire_lock(slug: str) -> bool:
    """Create lockfile. Returns False if already locked."""
    lp = lock_path(slug)
    if lp.exists():
        # Check if stale (>2 hours old)
        age = datetime.now().timestamp() - lp.stat().st_mtime
        if age > 7200:
            log.warning(f"Stale lockfile removed (age={age:.0f}s)")
            lp.unlink()
        else:
            log.warning(f"Project '{slug}' is locked (age={age:.0f}s)")
            return False
    lp.write_text(str(datetime.now().isoformat()))
    return True


def release_lock(slug: str) -> None:
    """Remove lockfile."""
    lp = lock_path(slug)
    if lp.exists():
        lp.unlink()


# ---------------------------------------------------------------------------
# List projects
# ---------------------------------------------------------------------------

def list_active_projects() -> list[str]:
    """Return slugs of projects that have incomplete stories and no active lock."""
    if not PROJECTS_DIR.exists():
        return []
    active = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        prd_file = d / "prd.json"
        lock_file = d / ".ralph.lock"
        if not prd_file.exists():
            continue
        if lock_file.exists():
            age = datetime.now().timestamp() - lock_file.stat().st_mtime
            if age < 7200:
                continue  # actively locked
        try:
            prd = json.loads(prd_file.read_text())
            if not all_done(prd):
                active.append(d.name)
        except Exception:
            pass
    return active
