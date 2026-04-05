import json
from pathlib import Path

POSITIONS_FILE = Path(__file__).parent.parent / 'data' / 'positions.json'

def load_positions() -> list:
    if not POSITIONS_FILE.exists(): return []
    try: return json.loads(POSITIONS_FILE.read_text())
    except: return []

def save_positions(positions: list):
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))

def add_position(pos: dict):
    positions = [p for p in load_positions() if p.get('symbol') != pos['symbol']]
    positions.append(pos)
    save_positions(positions)

def remove_position(symbol: str):
    save_positions([p for p in load_positions() if p.get('symbol') != symbol])

def get_position(symbol: str):
    return next((p for p in load_positions() if p.get('symbol') == symbol), None)
