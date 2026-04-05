import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

POSITIONS_FILE = Path(__file__).parent.parent / 'data' / 'range_positions.json'


def load_positions() -> list:
    if not POSITIONS_FILE.exists():
        return []
    try:
        with open(POSITIONS_FILE, 'r', encoding='ascii') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"Warning: Failed to parse {POSITIONS_FILE}: {e}")
        return []


def save_positions(positions: list):
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POSITIONS_FILE, 'w', encoding='ascii') as f:
        json.dump(positions, f, indent=2)


def add_position(pos: dict):
    positions = load_positions()
    positions = [p for p in positions if p['symbol'] != pos['symbol']]
    positions.append(pos)
    save_positions(positions)


def remove_position(symbol: str):
    positions = load_positions()
    positions = [p for p in positions if p['symbol'] != symbol]
    save_positions(positions)


def get_position(symbol: str):
    positions = load_positions()
    for p in positions:
        if p['symbol'] == symbol:
            return p
    return None
