import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import uuid
from datetime import datetime, timezone


DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_FILE = DATA_DIR / "positions.json"


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_positions() -> list:
    """Load positions from JSON file, return empty list if not exists."""
    if not POSITIONS_FILE.exists():
        return []
    with open(POSITIONS_FILE, "r") as f:
        return json.load(f)


def save_positions(positions: list) -> None:
    """Write positions to JSON file with indent=2."""
    _ensure_data_dir()
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


def add_position(pos: dict) -> None:
    """Append a new position to the positions file."""
    positions = load_positions()
    positions.append(pos)
    save_positions(positions)


def get_open_positions() -> list:
    """Filter and return positions with status == 'open'."""
    positions = load_positions()
    return [p for p in positions if p.get("status") == "open"]


def close_position(position_id: str, exit_price: float, exit_reason: str) -> None:
    """Update position status and exit fields."""
    positions = load_positions()
    for p in positions:
        if p.get("id") == position_id:
            p["status"] = "closed"
            p["exit_price"] = exit_price
            p["exit_reason"] = exit_reason
            p["closed_at_utc"] = _now_utc_iso()
            break
    save_positions(positions)


def count_open_positions() -> int:
    """Return count of open positions."""
    return len(get_open_positions())
