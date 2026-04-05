#!/usr/bin/env python3
"""
log_monitor.py - monitoring script that checks logs and sends Telegram alerts

Performs 5 checks:
1. Pipeline heartbeat - last run_log.jsonl entry within 35 minutes
2. Error scan - recent errors in cron.log
3. API errors - entries in api_error_log.jsonl within 2 hours
4. Verifier alarms - entries in verifier_alarms.jsonl within 2 hours
5. Credit budget - daily usage vs limit

Sends ONE consolidated Telegram message if any check fails.
Silent (no message) if all checks pass.

Usage:
  python3 scripts/log_monitor.py
"""

import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Load env from ~/.hermes/.env
env_path = Path.home() / '.hermes' / '.env'
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

DATA_DIR = Path(__file__).parent.parent / 'data'


def send_telegram(message: str) -> None:
    """Send a Telegram message via bot API to the trading group."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    group_id = '-5264050975'  # Trading group
    if not token or not group_id:
        return
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        requests.post(url, json={'chat_id': group_id, 'text': message}, timeout=10)
    except Exception:
        pass


def check_pipeline_heartbeat() -> str | None:
    """Check if pipeline has run within last 35 minutes."""
    run_log = DATA_DIR / 'run_log.jsonl'
    if not run_log.exists():
        return 'run_log.jsonl missing - pipeline may not have run yet'
    
    try:
        with open(run_log, 'r') as f:
            lines = f.readlines()
        if not lines:
            return 'run_log.jsonl missing - pipeline may not have run yet'
        
        last_line = lines[-1].strip()
        if not last_line:
            return 'run_log.jsonl missing - pipeline may not have run yet'
        
        entry = json.loads(last_line)
        ts_str = entry.get('ts')
        if not ts_str:
            return 'run_log.jsonl missing - pipeline may not have run yet'
        
        last_run = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        diff = now - last_run
        if diff.total_seconds() > 35 * 60:
            return f'Pipeline heartbeat missed: last run {ts_str}'
    except Exception:
        return 'run_log.jsonl missing - pipeline may not have run yet'
    
    return None


def check_error_scan() -> str | None:
    """Check cron.log for recent errors."""
    cron_log = DATA_DIR / 'cron.log'
    if not cron_log.exists():
        return None
    
    try:
        with open(cron_log, 'r') as f:
            lines = f.readlines()
        
        recent_lines = lines[-150:]
        error_patterns = ['Traceback', 'Error:', 'CRITICAL', 'Exception']
        matching = []
        
        for line in recent_lines:
            if any(p in line for p in error_patterns):
                matching.append(line.strip())
        
        if matching:
            return f'Errors in cron.log: {"||".join(matching[:3])}'
    except Exception:
        pass
    
    return None


def check_api_errors() -> str | None:
    """Check for Nansen API errors in last 2 hours."""
    api_log = DATA_DIR / 'api_error_log.jsonl'
    if not api_log.exists():
        return None
    
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        with open(api_log, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get('ts')
                    if not ts_str:
                        continue
                    entry_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    if entry_time >= cutoff:
                        error_msg = entry.get('error', 'unknown error')
                        return f'Nansen API errors (1 in last 2h): {error_msg}'
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    
    return None


def check_verifier_alarms() -> str | None:
    """Check for verifier alarms in last 2 hours."""
    alarm_log = DATA_DIR / 'verifier_alarms.jsonl'
    if not alarm_log.exists():
        return None
    
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        with open(alarm_log, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get('ts')
                    if not ts_str:
                        continue
                    entry_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    if entry_time >= cutoff:
                        alarm_text = entry.get('alarm', 'unknown alarm')
                        return f'Verifier alarm: {alarm_text}'
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    
    return None


def check_credit_budget() -> tuple[str | None, str | None]:
    """Check credit budget usage for today. Returns (alert, warn) or (None, None)."""
    credit_log = DATA_DIR / 'credit_log.jsonl'
    if not credit_log.exists():
        return None, None
    
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        total_used = 0
        
        with open(credit_log, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get('ts')
                    if not ts_str:
                        continue
                    entry_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    if entry_time.date().isoformat() != today:
                        continue
                    
                    credits_used = entry.get('credits_used')
                    if credits_used is None or credits_used < 0:
                        continue
                    total_used += credits_used
                except json.JSONDecodeError:
                    continue
    except Exception:
        return None, None
    
    limit = 133
    if total_used > limit:
        return f'Credit budget exceeded: {total_used} used today (limit {limit})', None
    elif total_used > 100:
        return None, f'WARN: Credit budget high: {total_used} used today (limit {limit})'
    
    return None, None


def main():
    alerts = []
    warns = []
    
    # Check 1: Pipeline heartbeat
    alert = check_pipeline_heartbeat()
    if alert:
        alerts.append(alert)
    
    # Check 2: Error scan
    alert = check_error_scan()
    if alert:
        alerts.append(alert)
    
    # Check 3: API errors
    alert = check_api_errors()
    if alert:
        alerts.append(alert)
    
    # Check 4: Verifier alarms
    alert = check_verifier_alarms()
    if alert:
        alerts.append(alert)
    
    # Check 5: Credit budget
    alert, warn = check_credit_budget()
    if alert:
        alerts.append(alert)
    if warn:
        warns.append(warn)
    
    # Send consolidated message if any alerts
    if alerts or warns:
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        lines = [f'[BASE TRADER MONITOR] {now}']
        for a in alerts:
            lines.append(a)
        for w in warns:
            lines.append(w)
        message = '\n'.join(lines)
        send_telegram(message)
        print(f'[MONITOR] Alert sent: {len(alerts)} alert(s), {len(warns)} warn(s)')
    else:
        print('[MONITOR] All checks passed')


if __name__ == '__main__':
    main()
