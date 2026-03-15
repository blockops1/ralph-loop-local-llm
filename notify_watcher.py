"""
notify_watcher.py — Drain ralph/notifications.txt and send to Telegram.

Called by cron every few minutes. Reads and clears the notifications file,
sends each line as a Telegram message via the Telegram bot API.

Usage: python3 notify_watcher.py

Requires environment variables:
  TELEGRAM_BOT_TOKEN — from @BotFather
  TELEGRAM_CHAT_ID   — your personal chat ID (from @userinfobot)
"""

import os
import sys
import requests
from pathlib import Path

RALPH_DIR = Path(__file__).parent
NOTIFY_FILE = RALPH_DIR / "notifications.txt"


def send_telegram(message: str) -> None:
    """Send message via Telegram bot API."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping", file=sys.stderr)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"Ralph: {message}"},
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def main():
    if not NOTIFY_FILE.exists():
        return
    lines = NOTIFY_FILE.read_text().strip().splitlines()
    NOTIFY_FILE.unlink()
    for line in lines:
        line = line.strip()
        if line:
            send_telegram(line)


if __name__ == "__main__":
    main()
