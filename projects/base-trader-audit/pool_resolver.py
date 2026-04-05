#!/usr/bin/env python3
"""
pool_resolver.py — Singleton DexScreener caller for the base-trader pipeline.

DexScreener is called ONCE per new token and the result is stored permanently in
pool_cache.json. All pipeline scripts (execute.py, scanner.py, gecko_terminal.py)
read from the cache instead of calling DexScreener directly.

Cache strategy:
  - Successfully resolved pools are PERMANENT — never re-queried.
  - Failed resolutions (no pool found) are retried on next cycle (10-min TTL).
  - New tokens get one DexScreener call, then cached forever.

Usage (pipeline):
    from pool_resolver import resolve_pools, get_pool_info
    pool_cache = resolve_pools([t['address'] for t in discovered_tokens])

Usage (execute.py):
    pool_info = get_pool_info(token_address)
    if not pool_info:
        pool_info = resolve_single(token_address)  # last-resort live call

Cache location: {base-trader}/data/pool_cache.json
API: DexScreener /latest/dex/tokens/{address} (no auth required)
"""

import json
import os
import sys
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Constants ─────────────────────────────────────────────────────────────────
WETH = '0x4200000000000000000000000000000000000006'
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
DEXSCREENER_URL = 'https://api.dexscreener.com/latest/dex/tokens'
CACHE_PATH = None  # Set at runtime based on caller location
FAILED_TTL_SECONDS = 600  # 10 minutes before retrying a failed resolution


def _cache_path() -> Path:
    """Return the pool_cache.json path. Uses caller dir to locate base-trader root."""
    if CACHE_PATH:
        return CACHE_PATH
    # Default to shared location alongside other base-trader data files
    base = Path(__file__).parent.parent
    return base / 'data' / 'pool_cache.json'


def _load_cache() -> dict:
    """Load existing cache, or return empty dict if file doesn't exist."""
    path = _cache_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {'_meta': {'resolved_at': None, 'resolved_count': 0, 'new_count': 0, 'failed_count': 0}}


def _save_cache(cache: dict) -> None:
    """Atomically write cache to disk."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(path)


def _dexscreener_lookup(token_addr: str) -> dict | None:
    """
    Query DexScreener for all pools of a given token on Base.
    Returns the best Aerodrome pool (highest liquidity) as a dict,
    or None if no Aerodrome pool exists.

    Returns dict:
      {
        'pool_address': str,
        'routing_token': 'WETH' | 'USDC',
        'quote_token_address': str,
        'liquidity_usd': float,
        'price_usd': float,
        'dex': 'aerodrome',
        'resolved_at': str (ISO),
        'status': 'resolved',
      }
    Returns None if no Aerodrome pool found.
    """
    url = f'{DEXSCREENER_URL}/{token_addr.lower()}'
    try:
        r = requests.get(url, headers={'Accept': 'application/json'}, timeout=15)
        if r.status_code != 200:
            print(f'[POOL_RESOLVER] DexScreener {r.status_code} for {token_addr}')
            return None
        data = r.json()
    except Exception as e:
        print(f'[POOL_RESOLVER] DexScreener error for {token_addr}: {e}')
        return None

    pairs = data.get('pairs', [])
    if isinstance(pairs, dict):
        pairs = pairs.get('pairs', [])
    if not pairs:
        return None

    token_addr_lower = token_addr.lower()

    # Filter to Aerodrome pools only
    aero_candidates = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        if p.get('dexId') != 'aerodrome':
            continue
        base = (p.get('baseToken', {}) or {}).get('address', '').lower()
        quote = (p.get('quoteToken', {}) or {}).get('address', '').lower()
        liq = float(p.get('liquidity', {}).get('usd', 0) or 0)
        price = float(p.get('priceUsd', 0) or 0)
        if liq <= 0:
            continue

        if base == token_addr_lower:
            our_side, other = 'base', quote
        elif quote == token_addr_lower:
            our_side, other = 'quote', base
        else:
            continue

        if other == WETH.lower():
            routing = 'WETH'
            quote_addr = WETH
        elif other == USDC.lower():
            routing = 'USDC'
            quote_addr = USDC
        else:
            routing = other[:10] + '...'
            quote_addr = other

        aero_candidates.append({
            'pool_address': p.get('pairAddress', ''),
            'routing_token': routing,
            'quote_token_address': quote_addr,
            'liquidity_usd': liq,
            'price_usd': price,
            'dex': 'aerodrome',
        })

    if not aero_candidates:
        return None

    # Sort by liquidity, take the best
    aero_candidates.sort(key=lambda x: x['liquidity_usd'], reverse=True)
    best = aero_candidates[0]
    best['resolved_at'] = datetime.now(timezone.utc).isoformat()
    best['status'] = 'resolved'
    return best


def resolve_single(token_addr: str) -> dict | None:
    """
    Force-resolve a single token via DexScreener. Returns None if no Aerodrome pool.
    Use this for on-demand resolution when cache is empty.
    Writes a failed entry to cache so retry TTL applies on subsequent calls.
    """
    result = _dexscreener_lookup(token_addr)
    cache = _load_cache()
    addr_key = token_addr.lower()
    if result:
        cache[addr_key] = result
        cache['_meta']['resolved_at'] = datetime.now(timezone.utc).isoformat()
        cache['_meta']['resolved_count'] = cache['_meta'].get('resolved_count', 0) + 1
    else:
        # Write failed entry so TTL retry logic applies on next call
        if addr_key not in cache or cache.get(addr_key, {}).get('status') != 'failed':
            cache[addr_key] = {
                'pool_address': None,
                'routing_token': None,
                'quote_token_address': None,
                'liquidity_usd': 0.0,
                'price_usd': 0.0,
                'dex': None,
                'resolved_at': datetime.now(timezone.utc).isoformat(),
                'status': 'failed',
            }
    _save_cache(cache)
    return result


def get_pool_info(token_addr: str) -> dict | None:
    """
    Read pool info for a single token from the cache.
    Returns None if token is not in cache.
    """
    cache = _load_cache()
    return cache.get(token_addr.lower())


def resolve_pools(token_addresses: list[str]) -> dict:
    """
    Resolve Aerodrome pool info for a list of token addresses.
    Only queries DexScreener for tokens NOT already in cache,
    or tokens whose last resolution failed (and 10+ minutes have passed).

    Returns the full pool_cache dict after merging new resolutions.

    Args:
        token_addresses: list of token contract addresses (checksum or lowercase)

    Returns:
        Full pool_cache dict (including all previously resolved tokens)
    """
    token_addresses = [a.lower() for a in token_addresses]
    cache = _load_cache()
    now = time.time()

    new_count = 0
    failed_count = 0
    skipped_count = 0

    for addr in token_addresses:
        existing = cache.get(addr)

        if existing and existing.get('status') == 'resolved':
            skipped_count += 1
            continue

        # Has a failed entry — check TTL
        if existing and existing.get('status') == 'failed':
            resolved_at = existing.get('resolved_at', '')
            try:
                ts = datetime.fromisoformat(resolved_at).timestamp()
                if now - ts < FAILED_TTL_SECONDS:
                    skipped_count += 1
                    continue
            except (ValueError, OSError):
                pass
            # TTL expired, retry

        # Resolve via DexScreener
        print(f'[POOL_RESOLVER] Resolving {addr}...')
        result = _dexscreener_lookup(addr)

        if result:
            cache[addr] = result
            new_count += 1
        else:
            # No Aerodrome pool found — store as failed with timestamp
            cache[addr] = {
                'pool_address': None,
                'routing_token': None,
                'quote_token_address': None,
                'liquidity_usd': 0.0,
                'price_usd': 0.0,
                'dex': None,
                'resolved_at': datetime.now(timezone.utc).isoformat(),
                'status': 'failed',
            }
            failed_count += 1

        # Rate-limit: be nice to DexScreener between calls
        time.sleep(0.25)

    # Update meta
    cache['_meta'] = {
        'resolved_at': datetime.now(timezone.utc).isoformat(),
        'resolved_count': cache['_meta'].get('resolved_count', 0) + new_count,
        'new_count': new_count,
        'failed_count': failed_count,
        'skipped_count': skipped_count,
        'total_tokens': len(cache) - 1,  # subtract _meta key
    }

    _save_cache(cache)
    print(f'[POOL_RESOLVER] Done — {new_count} resolved, {skipped_count} cached, {failed_count} failed')
    return cache


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Resolve Aerodrome pools for tokens')
    parser.add_argument('token', nargs='?', help='Single token address to resolve')
    parser.add_argument('--tokens', nargs='*', help='Multiple token addresses')
    parser.add_argument('--list', action='store_true', help='List all cached tokens')
    parser.add_argument('--clear', action='store_true', help='Clear failed entries (retry them)')
    args = parser.parse_args()

    if args.clear:
        cache = _load_cache()
        for addr, entry in list(cache.items()):
            if addr != '_meta' and entry.get('status') == 'failed':
                del cache[addr]
        _save_cache(cache)
        print(f'[POOL_RESOLVER] Cleared failed entries.')
        sys.exit(0)

    if args.list:
        cache = _load_cache()
        print(f'Pool cache ({len(cache) - 1} tokens):')
        for addr, entry in cache.items():
            if addr == '_meta':
                continue
            status = entry.get('status', '?')
            pool = entry.get('pool_address', 'none')
            liq = entry.get('liquidity_usd', 0)
            print(f'  {addr} | {status} | pool={pool[:20] if pool else "none"}... | liq=${liq:,.0f}')
        print(f'Meta: {cache.get("_meta", {})}')
        sys.exit(0)

    addresses = []
    if args.token:
        addresses = [args.token]
    elif args.tokens:
        addresses = args.tokens

    if not addresses:
        parser.print_help()
        sys.exit(0)

    for addr in addresses:
        result = resolve_single(addr)
        if result:
            print(f'  pool={result["pool_address"]}')
            print(f'  routing={result["routing_token"]}')
            print(f'  liquidity=${result["liquidity_usd"]:,.0f}')
        else:
            print(f'  No Aerodrome pool found')
