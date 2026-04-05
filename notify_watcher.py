"""
notify_watcher.py — Drain ralph/notifications.txt and send to Discord.

Called by cron every few minutes. Reads and clears the notifications file,
sends each line as a Discord message via Bot API.

Usage: python3 notify_watcher.py
"""

import os
import sys
import requests
from pathlib import Path

RALPH_DIR = Path(__file__).parent
NOTIFY_FILE = RALPH_DIR / "notifications.txt"


def send_discord(message: str) -> None:
    """Send message via Discord Bot API."""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set")
        return
    channel_id = "1488012271044132944"
    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json={"content": message},
            timeout=15,
        )
        if resp.ok:
            print(f"[OK] Discord notification sent")
        else:
            print(f"[ERROR] Discord API returned {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"Discord send failed: {e}")


def main():
    if not NOTIFY_FILE.exists():
        return

    content = NOTIFY_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return

    # Clear file atomically
    NOTIFY_FILE.write_text("")

    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return

    # Batch into one message if multiple
    if len(lines) == 1:
        send_discord(lines[0])
    else:
        combined = "📊 Ralph updates:\n" + "\n".join(lines)
        send_discord(combined)

    print(f"Sent {len(lines)} notification(s)")


if __name__ == "__main__":
    main()
