#!/usr/bin/env python3
"""
loop_runner.py — Auto-loop through all pending stories.

Provides run_all_stories() which iterates through all pending stories
in the PRD until done, blocked, or max_iterations reached.
"""

import sys
from pathlib import Path

# Allow imports from sibling modules
sys.path.insert(0, str(Path(__file__).parent))


def run_all_stories(args, cfg, log, prd, slug):
    """
    Iterate through all pending stories in prd until done, blocked, or max_iterations reached.
    Called by _run() in ralph.py. Returns the final prd dict.
    """
    from prd_manager import (
        get_next_story, all_done, any_blocked,
        mark_story_done, mark_story_failed,
        save_prd, append_progress, story_summary
    )
    max_iter = cfg.get('max_iterations', 20)
    iteration = 0
    while iteration < max_iter:
        # If --story was specified, only run that one story
        if args.story:
            if iteration > 0:
                log.info('--story flag: single-story mode, stopping after first iteration')
                break
        story = get_next_story(prd, max_attempts=cfg.get('max_attempts_per_story', 3))
        if story is None:
            log.info('No more pending stories — done.')
            break
        story['_slug'] = slug
        log.info(f"[{iteration+1}/{max_iter}] Story: {story['id']} — {story['title']}")
        log.info(f'PRD status: {story_summary(prd)}')
        # Import run_story_loop, run_quality_checks, notify from ralph module
        import ralph as _ralph_mod
        success, summary = _ralph_mod.run_story_loop(story, cfg, log, dry_run=getattr(args, 'dry_run', False))
        if success:
            checks_passed, checks_output = _ralph_mod.run_quality_checks(story, log)
            if not checks_passed:
                log.error(f'Quality checks failed:\n{checks_output}')
                prd = mark_story_failed(prd, story['id'], 'Quality checks failed')
                save_prd(prd, slug)
                append_progress(slug, f"❌ {story['id']} FAILED\n{checks_output[:300]}")
                iteration += 1
                continue
            from tools import tool_git_commit
            commit_result = tool_git_commit(f"feat: {story['id']} — {story['title']}")
            commit_str = str(commit_result)
            _cs = commit_str.lower()
            _no_changes = (
                'nothing to commit' in _cs
                or 'nothing added to commit' in _cs
                or 'no changes added to commit' in _cs
                or 'working tree clean' in _cs
                or commit_str.startswith('OK: Nothing to commit')
            )
            git_ok = _no_changes or 'EXIT CODE: 0' in commit_str
            if not git_ok:
                log.error(f'Git commit failed:\n{commit_result}')
                prd = mark_story_failed(prd, story['id'], 'Git commit failed')
                save_prd(prd, slug)
                append_progress(slug, f"❌ {story['id']} FAILED\nGit commit failed")
                iteration += 1
                continue
            prd = mark_story_done(prd, story['id'], summary[:200])
            save_prd(prd, slug)
            append_progress(slug, f"✅ {story['id']}: {story['title']}\n{summary[:300]}")
            log.info(f"Story {story['id']} marked complete")
            _ralph_mod.notify(f"✅ {story['id']} done in '{slug}'", log)
        else:
            prd = mark_story_failed(prd, story['id'], summary)
            save_prd(prd, slug)
            append_progress(slug, f"❌ {story['id']} FAILED\n{summary[:300]}")
            log.error(f"Story {story['id']} failed: {summary[:100]}")
            _ralph_mod.notify(f"❌ {story['id']} FAILED in '{slug}': {summary[:120]}", log)
        iteration += 1
    return prd
