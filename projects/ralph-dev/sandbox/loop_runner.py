#!/usr/bin/env python3
"""
loop_runner.py -- Auto-loop through all pending stories via subprocess.

Provides run_all_stories() which iterates through all pending stories
in the PRD until done, blocked, or max_iterations reached. Each story
is run in a fresh subprocess.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

# Allow imports from sibling modules
sys.path.insert(0, str(Path(__file__).parent))

from prd_manager import (
    get_next_story, all_done, any_blocked,
    mark_story_done, mark_story_failed,
    save_prd, append_progress, story_summary
)
from tools import tool_git_commit


def run_all_stories(args, cfg, log, prd, slug):
    """
    Iterate through all pending stories in prd until done, blocked, or max_iterations reached.
    Each story is run in a fresh subprocess. Returns the final prd dict.
    """
    max_iter = cfg.get('max_iterations', 20)
    iteration = 0
    while iteration < max_iter:
        # If --story was specified, only run that one story
        if args.story:
            if iteration > 0:
                log.info('--story flag: single-story mode, stopping after first iteration')
                break
        
        # Check for single-story mode flags in PRD (set by ralph.py --single-story)
        prd_flags = prd.get('flags', {})
        if prd_flags.get('story') and prd_flags.get('single_story'):
            log.info('Single-story mode detected via flags, skipping subprocess loop')
            break
        
        story = get_next_story(prd, max_attempts=cfg.get('max_attempts_per_story', 3))
        if story is None:
            log.info('No more pending stories -- done.')
            break
        
        story['_slug'] = slug
        story_title = story.get('title', story['id'])
        log.info(f"[{iteration+1}/{max_iter}] Story: {story['id']} -- {story_title}")
        log.info(f'PRD status: {story_summary(prd)}')
        
        # Spawn subprocess for this story
        sidecar_path = f'/tmp/ralph-story-result-{slug}-{story["id"]}.json'
        Path(sidecar_path).unlink(missing_ok=True)
        
        cmd = [
            sys.executable,
            str(Path(__file__).parent / 'ralph.py'),
            slug,
            '--story', story['id'],
            '--single-story',
            '--config', cfg.get('config_path', str(Path(__file__).parent / 'config.yaml'))
        ]
        result = subprocess.run(cmd, cwd=str(Path(__file__).parent.parent), timeout=cfg.get('story_timeout', 3600))
        
        # Read sidecar JSON
        sidecar_success = False
        sidecar_summary = 'sidecar not found'
        sidecar_elapsed = 0.0
        
        try:
            with open(sidecar_path, 'r') as f:
                sidecar = json.load(f)
                sidecar_success = sidecar.get('success', False)
                sidecar_summary = sidecar.get('summary', 'sidecar not found')
                sidecar_elapsed = sidecar.get('elapsed', 0.0)
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            sidecar_summary = 'sidecar not found'
        
        # success = (result.returncode == 0) AND sidecar['success'] == True
        success = (result.returncode == 0) and sidecar_success
        
        story_elapsed = sidecar_elapsed if sidecar_elapsed > 0 else time.time() - time.time()  # Use sidecar elapsed
        
        log.info(f"{'' if success else ''} Story {story['id']} finished (returncode={result.returncode}, sidecar_success={sidecar_success})")
        
        if success:
            prd = mark_story_done(prd, story['id'], sidecar_summary[:200])
            save_prd(prd, slug)
            append_progress(slug, f" {story['id']}: {story_title}\n{sidecar_summary[:300]}")
            log.info(f"Story {story['id']} marked complete")
            commit_result = tool_git_commit(f"feat: {story['id']} -- {story_title}")
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
                append_progress(slug, f" {story['id']} FAILED\nGit commit failed")
                iteration += 1
                continue
            log.info(f"Story {story['id']} marked complete")
        else:
            prd = mark_story_failed(prd, story['id'], sidecar_summary)
            save_prd(prd, slug)
            append_progress(slug, f" {story['id']} FAILED\n{sidecar_summary[:300]}")
            log.error(f"Story {story['id']} failed: {sidecar_summary[:100]}")
        
        iteration += 1
    return prd
