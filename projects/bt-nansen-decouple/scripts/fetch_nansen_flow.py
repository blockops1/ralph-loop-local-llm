#!/usr/bin/env python3
"""
fetch_nansen_flow.py - Standalone Nansen Flow Intelligence fetcher

Fetches flow intelligence data for tracked tokens and stores as time-series cache.
Designed to run via cron or direct call.

Features:
  - Reads tracked tokens from data/sm_holdings_cache.json (fallback to config/watchlist.json)
  - Throttle: skips tokens fetched within the last hour
  - Budget guard: checks daily credit budget before making API calls
  - Time-series cache: appends snapshots, prunes data older than 30 days
  - Discord notification: summary of tokens updated, credits used, errors

Usage:
  python3 scripts/fetch_nansen_flow.py

Environment variables:
  NANSEN_API_KEY: Nansen API key (required for non-mock mode)
  NANSEN_MOCK: Set to '1' to run in mock mode (no API calls)
  DISCORD_BOT_TOKEN: Discord bot token for notifications (optional)
"""

import os
import sys
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# ============================================================================
# Configuration
# ============================================================================

# Script location and data directories
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / 'data'
SHARED_DIR = PROJECT_DIR.parent / 'shared'

# Cache files
SM_HOLDINGS_CACHE_FILE = DATA_DIR / 'sm_holdings_cache.json'
FLOW_INTELLIGENCE_CACHE_FILE = DATA_DIR / 'flow_intelligence_cache.json'
FLOW_INTELLIGENCE_CACHE_TMP = DATA_DIR / 'flow_intelligence_cache.json.tmp'

# Throttle: skip tokens fetched within this window (in seconds)
THROTTLE_SECONDS = 3600  # 1 hour

# Retention: prune snapshots older than this (in seconds)
RETENTION_SECONDS = 30 * 24 * 3600  # 30 days

# Credit budget: max credits to spend per day
DAILY_CREDIT_BUDGET = 500  # Adjust based on your Nansen plan

# Credits per flow-intelligence API call
CREDITS_PER_CALL = 10

# Discord notification channel
DISCORD_CHANNEL_ID = "1488012271044132944"


# ============================================================================
# Utility Functions
# ============================================================================

def log_info(msg: str) -> None:
    """Print info message with timestamp."""
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    print(f"[{ts}] INFO: {msg}")


def log_error(msg: str) -> None:
    """Print error message with timestamp."""
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    print(f"[{ts}] ERROR: {msg}")


def log_warn(msg: str) -> None:
    """Print warning message with timestamp."""
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    print(f"[{ts}] WARN: {msg}")


def _is_mock_mode() -> bool:
    """Check if mock mode is active."""
    return os.environ.get('NANSEN_MOCK') == '1' or not os.environ.get('NANSEN_API_KEY')


def _headers() -> dict:
    """Return correct Nansen auth headers."""
    return {
        'apikey': os.environ['NANSEN_API_KEY'],
        'Content-Type': 'application/json',
    }


def send_discord_notification(message: str) -> bool:
    """Send message to Discord via Bot API."""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        log_warn("DISCORD_BOT_TOKEN not set - skipping notification")
        return False
    
    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            json={"content": message},
            timeout=15,
        )
        if resp.ok:
            log_info("Discord notification sent")
            return True
        else:
            log_error(f"Discord API returned {resp.status_code}: {resp.text[:100]}")
            return False
    except Exception as e:
        log_error(f"Discord send failed: {e}")
        return False


# ============================================================================
# Budget Guard
# ============================================================================

def check_daily_budget() -> tuple[bool, str, int]:
    """
    Check if we have remaining daily credit budget.
    
    Returns:
        (budget_ok, message, remaining_budget)
    """
    # Track budget in a simple file
    budget_file = DATA_DIR / 'nansen_budget_tracker.json'
    
    try:
        if budget_file.exists():
            data = json.loads(budget_file.read_text())
            last_reset = data.get('last_reset_date', '')
            credits_used = data.get('credits_used_today', 0)
            
            # Check if we need to reset (new day)
            today = datetime.now(timezone.utc).date().isoformat()
            if last_reset != today:
                credits_used = 0
                data = {'last_reset_date': today, 'credits_used_today': 0}
        else:
            credits_used = 0
            data = {'last_reset_date': datetime.now(timezone.utc).date().isoformat(), 'credits_used_today': 0}
    except Exception as e:
        log_warn(f"Budget tracker error: {e} - starting fresh")
        credits_used = 0
        data = {'last_reset_date': datetime.now(timezone.utc).date().isoformat(), 'credits_used_today': 0}
    
    remaining = DAILY_CREDIT_BUDGET - credits_used
    
    if remaining < CREDITS_PER_CALL:
        return False, f"Daily budget exceeded: {credits_used}/{DAILY_CREDIT_BUDGET} credits used", 0
    
    return True, f"Budget OK: {credits_used}/{DAILY_CREDIT_BUDGET} credits used ({remaining} remaining)", remaining


def update_budget_tracker(credits_spent: int) -> None:
    """Update the daily budget tracker after API calls."""
    budget_file = DATA_DIR / 'nansen_budget_tracker.json'
    
    try:
        if budget_file.exists():
            data = json.loads(budget_file.read_text())
        else:
            data = {'last_reset_date': datetime.now(timezone.utc).date().isoformat(), 'credits_used_today': 0}
        
        # Reset if new day
        today = datetime.now(timezone.utc).date().isoformat()
        if data.get('last_reset_date') != today:
            data = {'last_reset_date': today, 'credits_used_today': 0}
        
        data['credits_used_today'] = data.get('credits_used_today', 0) + credits_spent
        
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        budget_file.write_text(json.dumps(data))
        log_info(f"Budget updated: {data['credits_used_today']}/{DAILY_CREDIT_BUDGET} credits used today")
    except Exception as e:
        log_warn(f"Failed to update budget tracker: {e}")


# ============================================================================
# Token Discovery
# ============================================================================

def get_tracked_tokens() -> list[str]:
    """
    Get list of tracked token addresses.
    
    Priority:
      1. data/sm_holdings_cache.json (preferred - tokens SM wallets are holding)
      2. config/watchlist.json (fallback)
    
    Returns:
        List of token addresses (lowercase)
    """
    tokens = []
    
    # Priority 1: SM holdings cache
    if SM_HOLDINGS_CACHE_FILE.exists():
        try:
            data = json.loads(SM_HOLDINGS_CACHE_FILE.read_text())
            holdings = data.get('holdings', {})
            if isinstance(holdings, dict):
                tokens = [addr.lower() for addr in holdings.keys()]
                log_info(f"Loaded {len(tokens)} tokens from sm_holdings_cache.json")
                return tokens
        except Exception as e:
            log_warn(f"Failed to read sm_holdings_cache.json: {e}")
    
    # Fallback: watchlist.json
    watchlist_file = PROJECT_DIR.parent / 'config' / 'watchlist.json'
    if watchlist_file.exists():
        try:
            data = json.loads(watchlist_file.read_text())
            if isinstance(data, list):
                tokens = [t.lower() if isinstance(t, str) else t.get('address', '').lower() for t in data]
                tokens = [t for t in tokens if t]  # Filter empty
                log_info(f"Loaded {len(tokens)} tokens from watchlist.json (fallback)")
                return tokens
        except Exception as e:
            log_warn(f"Failed to read watchlist.json: {e}")
    
    log_warn("No tracked tokens found in any source")
    return []


# ============================================================================
# Flow Intelligence Cache (Time-Series)
# ============================================================================

def load_flow_cache() -> dict:
    """
    Load flow intelligence time-series cache.
    
    Cache format (v3):
    {
      "_index": {
        "version": 3,
        "last_full_fetch": "ISO timestamp",
        "tokens": ["addr1", "addr2", ...]
      },
      "tokens": {
        "addr1": {
          "snapshots": [
            {"ts": "ISO", "total_net_flow_usd": float, "segments": {...}},
            ...
          ]
        }
      }
    }
    """
    if not FLOW_INTELLIGENCE_CACHE_FILE.exists():
        return {
            "_index": {"version": 3, "last_full_fetch": None, "tokens": []},
            "tokens": {}
        }
    
    try:
        data = json.loads(FLOW_INTELLIGENCE_CACHE_FILE.read_text())
        # Validate structure
        if 'tokens' not in data:
            log_warn("Cache missing 'tokens' key - initializing fresh")
            return {
                "_index": {"version": 3, "last_full_fetch": None, "tokens": []},
                "tokens": {}
            }
        return data
    except Exception as e:
        log_warn(f"Failed to load cache: {e} - initializing fresh")
        return {
            "_index": {"version": 3, "last_full_fetch": None, "tokens": []},
            "tokens": {}
        }


def save_flow_cache(cache: dict) -> None:
    """Save flow intelligence cache atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        FLOW_INTELLIGENCE_CACHE_TMP.write_text(json.dumps(cache, indent=2))
        FLOW_INTELLIGENCE_CACHE_TMP.replace(FLOW_INTELLIGENCE_CACHE_FILE)
    except Exception as e:
        log_error(f"Failed to save cache: {e}")
        raise


def get_last_fetch_time(cache: dict, token_address: str) -> Optional[datetime]:
    """Get the timestamp of the last snapshot for a token."""
    token_data = cache.get('tokens', {}).get(token_address, {})
    snapshots = token_data.get('snapshots', [])
    if not snapshots:
        return None
    # Get most recent snapshot
    latest = snapshots[-1]
    ts_str = latest.get('ts', '')
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except Exception:
        return None


def should_skip_throttle(cache: dict, token_address: str) -> bool:
    """
    Check if token should be skipped due to throttle.
    
    Returns True if token was fetched within THROTTLE_SECONDS.
    """
    last_fetch = get_last_fetch_time(cache, token_address)
    if not last_fetch:
        return False  # No previous fetch - should fetch
    
    age = (datetime.now(timezone.utc) - last_fetch).total_seconds()
    if age < THROTTLE_SECONDS:
        log_info(f"Throttling {token_address[:16]}... (fetched {int(age)}s ago)")
        return True
    return False


def prune_old_snapshots(cache: dict) -> int:
    """
    Remove snapshots older than RETENTION_SECONDS from all tokens.
    
    Returns:
        Number of snapshots pruned
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RETENTION_SECONDS)
    pruned_count = 0
    
    for addr, token_data in cache.get('tokens', {}).items():
        snapshots = token_data.get('snapshots', [])
        original_len = len(snapshots)
        
        # Keep snapshots newer than cutoff
        filtered = []
        for snap in snapshots:
            ts_str = snap.get('ts', '')
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts >= cutoff:
                    filtered.append(snap)
            except Exception:
                # Invalid timestamp - keep it to be safe
                filtered.append(snap)
        
        token_data['snapshots'] = filtered
        pruned_count += (original_len - len(filtered))
    
    if pruned_count > 0:
        log_info(f"Pruned {pruned_count} snapshots older than 30 days")
    
    return pruned_count


def append_snapshot(cache: dict, token_address: str, snapshot: dict) -> None:
    """Append a new snapshot to a token's time series."""
    if 'tokens' not in cache:
        cache['tokens'] = {}
    
    if token_address not in cache['tokens']:
        cache['tokens'][token_address] = {'snapshots': []}
        # Update index
        if '_index' not in cache:
            cache['_index'] = {'version': 3, 'last_full_fetch': None, 'tokens': []}
        if token_address not in cache['_index']['tokens']:
            cache['_index']['tokens'].append(token_address)
    
    cache['tokens'][token_address]['snapshots'].append(snapshot)
    cache['_index']['last_full_fetch'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


# ============================================================================
# Nansen API
# ============================================================================

def fetch_flow_intelligence(token_address: str, chain: str = 'base') -> Optional[dict]:
    """
    Fetch flow intelligence data from Nansen API.
    
    Endpoint: POST /api/v1/tgm/flow-intelligence
    Credits: 10 per call
    
    Returns:
        Parsed flow data dict or None on error
    """
    if _is_mock_mode():
        log_info(f"[MOCK] Fetching flow intelligence for {token_address[:16]}...")
        return {
            'total_net_flow_usd': 125000.0,
            'net_flow_1h_usd': 50000.0,
            'net_flow_24h_usd': 125000.0,
            'net_flow_7d_usd': 300000.0,
            'net_flow_30d_usd': 800000.0,
            'buy_pressure_pct': 65.0,
            'sell_pressure_pct': 35.0,
            'segments': {
                'smart_money': {'net_flow_usd': 100000.0, 'buy_count': 15, 'sell_count': 5},
                'whales': {'net_flow_usd': 25000.0, 'buy_count': 3, 'sell_count': 2},
            }
        }
    
    try:
        r = requests.post(
            'https://api.nansen.ai/api/v1/tgm/flow-intelligence',
            headers=_headers(),
            json={'token_address': token_address, 'chain': chain},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get('data', {})
        
        # Parse response - handle both flat and segment-keyed formats
        result = {
            'total_net_flow_usd': float(data.get('total_net_flow_usd', data.get('net_flow_24h_usd', 0)) or 0),
            'net_flow_1h_usd': float(data.get('net_flow_1h_usd', 0) or 0),
            'net_flow_24h_usd': float(data.get('net_flow_24h_usd', 0) or 0),
            'net_flow_7d_usd': float(data.get('net_flow_7d_usd', 0) or 0),
            'net_flow_30d_usd': float(data.get('net_flow_30d_usd', 0) or 0),
            'buy_pressure_pct': float(data.get('buy_pressure_pct', 0) or 0),
            'sell_pressure_pct': float(data.get('sell_pressure_pct', 0) or 0),
            'segments': data.get('segments', {}),
        }
        
        log_info(f"Fetched {token_address[:16]}... (10 credits) - net_flow_24h: ${result['net_flow_24h_usd']:,.0f}")
        return result
        
    except requests.exceptions.RequestException as e:
        log_error(f"API request failed for {token_address[:16]}...: {e}")
        return None
    except Exception as e:
        log_error(f"Error parsing response for {token_address[:16]}...: {e}")
        return None


# ============================================================================
# Main Fetch Logic
# ============================================================================

def run_fetch() -> dict:
    """
    Main fetch routine.
    
    Returns:
        Summary dict with tokens_updated, credits_used, errors
    """
    log_info("=" * 60)
    log_info("Starting Nansen Flow Intelligence fetch")
    log_info("=" * 60)
    
    # Check budget before any API calls
    budget_ok, budget_msg, remaining = check_daily_budget()
    log_info(budget_msg)
    
    if not budget_ok:
        log_error("Daily credit budget exceeded - aborting fetch")
        return {
            'tokens_updated': 0,
            'credits_used': 0,
            'errors': [],
            'skipped_throttle': 0,
            'budget_exceeded': True,
        }
    
    # Load tracked tokens
    tokens = get_tracked_tokens()
    if not tokens:
        log_error("No tracked tokens found - aborting")
        return {
            'tokens_updated': 0,
            'credits_used': 0,
            'errors': [],
            'skipped_throttle': 0,
            'no_tokens': True,
        }
    
    log_info(f"Processing {len(tokens)} tracked tokens")
    
    # Load cache
    cache = load_flow_cache()
    
    # Track results
    tokens_updated = 0
    credits_used = 0
    errors = []
    skipped_throttle = 0
    
    # Process each token
    for token_address in tokens:
        # Check throttle
        if should_skip_throttle(cache, token_address):
            skipped_throttle += 1
            continue
        
        # Check remaining budget
        budget_ok, budget_msg, remaining = check_daily_budget()
        if not budget_ok:
            log_warn(f"Budget exceeded mid-fetch - stopping at {token_address[:16]}...")
            break
        
        # Fetch from API
        flow_data = fetch_flow_intelligence(token_address, chain='base')
        
        if flow_data is None:
            errors.append({
                'token': token_address,
                'error': 'API fetch failed',
            })
            continue
        
        # Create snapshot
        snapshot = {
            'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'total_net_flow_usd': flow_data['total_net_flow_usd'],
            'net_flow_1h_usd': flow_data['net_flow_1h_usd'],
            'net_flow_24h_usd': flow_data['net_flow_24h_usd'],
            'net_flow_7d_usd': flow_data['net_flow_7d_usd'],
            'net_flow_30d_usd': flow_data['net_flow_30d_usd'],
            'buy_pressure_pct': flow_data['buy_pressure_pct'],
            'sell_pressure_pct': flow_data['sell_pressure_pct'],
            'segments': flow_data.get('segments', {}),
        }
        
        # Append to cache
        append_snapshot(cache, token_address, snapshot)
        tokens_updated += 1
        credits_used += CREDITS_PER_CALL
    
    # Prune old snapshots
    pruned = prune_old_snapshots(cache)
    
    # Save cache
    try:
        save_flow_cache(cache)
        log_info(f"Cache saved: {len(cache.get('tokens', {}))} tokens")
    except Exception as e:
        log_error(f"Failed to save cache: {e}")
        errors.append({'token': 'cache', 'error': str(e)})
    
    # Update budget tracker
    if credits_used > 0:
        update_budget_tracker(credits_used)
    
    # Build summary
    summary = {
        'tokens_updated': tokens_updated,
        'credits_used': credits_used,
        'errors': errors,
        'skipped_throttle': skipped_throttle,
        'pruned_snapshots': pruned,
        'total_tokens': len(tokens),
    }
    
    log_info("=" * 60)
    log_info(f"Fetch complete: {tokens_updated} tokens updated, {credits_used} credits used")
    log_info(f"Skipped (throttle): {skipped_throttle}, Errors: {len(errors)}")
    log_info("=" * 60)
    
    return summary


def send_summary_notification(summary: dict) -> None:
    """Send Discord notification with fetch summary."""
    if summary.get('budget_exceeded'):
        msg = "🚫 **Nansen Fetch Skipped**\nDaily credit budget exceeded."
    elif summary.get('no_tokens'):
        msg = "⚠️ **Nansen Fetch Skipped**\nNo tracked tokens found."
    else:
        lines = [
            "📊 **Nansen Flow Intelligence Fetch**",
            f"✅ Tokens updated: {summary['tokens_updated']}",
            f"💰 Credits used: {summary['credits_used']}",
            f"⏭️ Skipped (throttle): {summary['skipped_throttle']}",
            f"🗑️ Pruned snapshots: {summary.get('pruned_snapshots', 0)}",
        ]
        
        if summary['errors']:
            lines.append(f"❌ Errors: {len(summary['errors'])}")
            for err in summary['errors'][:3]:  # Show first 3 errors
                token = err.get('token', 'unknown')[:16]
                error = err.get('error', 'unknown')[:50]
                lines.append(f"   * {token}...: {error}")
            if len(summary['errors']) > 3:
                lines.append(f"   ... and {len(summary['errors']) - 3} more")
        
        msg = "\n".join(lines)
    
    send_discord_notification(msg)


# ============================================================================
# Entry Point
# ============================================================================

def main() -> int:
    """Main entry point."""
    try:
        # Ensure data directory exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # Run fetch
        summary = run_fetch()
        
        # Send notification
        send_summary_notification(summary)
        
        # Exit code: 0 on success, 1 on errors
        if summary['errors']:
            return 1
        return 0
        
    except Exception as e:
        log_error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
