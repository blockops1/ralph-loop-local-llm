"""
notify_watcher.py — Drain ralph/notifications.txt and send to Telegram.

Called by cron every few minutes. Reads and clears the notifications file,
sends each line as a Telegram message via openclaw message tool pattern.

Usage: python3 notify_watcher.py
"""

import os
import sys
import subprocess
from pathlib import Path

RALPH_DIR = Path(__file__).parent
NOTIFY_FILE = RALPH_DIR / "notifications.txt"


def send_telegram(message: str) -> None:
    """Send message via openclaw CLI."""
    try:
        subprocess.run(
            ["openclaw", "message", "send", "--target", "374999219", "--message", message],
            capture_output=True,
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")


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
        send_telegram(lines[0])
    else:
        combined = "📊 Ralph updates:\n" + "\n".join(lines)
        send_telegram(combined)

    print(f"Sent {len(lines)} notification(s)")


if __name__ == "__main__":
    main()
