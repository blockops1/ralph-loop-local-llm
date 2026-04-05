"""
clear_ohlcv_cache.py - One-time migration script to clear OHLCV and price caches.

After US-001 changed currency to 'token', the USD candles in the cache must be
cleared so fresh ETH candles are fetched on the next scan.
"""

import json
import sys
from pathlib import Path

# Add parent directory to path for imports if needed
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'

GT_OHLCV_CACHE_FILE = DATA_DIR / 'gt_ohlcv_cache.json'
GT_PRICE_CACHE_FILE = DATA_DIR / 'gt_price_cache.json'


def clear_cache(file_path: Path) -> None:
    """Read cache file, write empty dict, print confirmation."""
    try:
        cache = json.loads(file_path.read_text())
    except FileNotFoundError:
        cache = {}
    except Exception:
        cache = {}

    file_path.write_text(json.dumps({}))
    print(f'Cache cleared: {file_path.name}')


def main():
    """Clear both OHLCV and price caches."""
    clear_cache(GT_OHLCV_CACHE_FILE)
    if GT_PRICE_CACHE_FILE.exists():
        clear_cache(GT_PRICE_CACHE_FILE)
    print('Cache cleared')


if __name__ == '__main__':
    main()
