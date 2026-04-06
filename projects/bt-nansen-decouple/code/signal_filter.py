"""
signal_filter.py - 4-pillar scoring engine with hard disqualifiers

This module provides the score_signal() function that evaluates tokens based on
four pillars: Price Structure, Smart Money Signal, Volume, and Token Health.
It applies hard disqualifiers first, then calculates pillar scores.
"""

# Minimum pool liquidity for execution (USD).
# Tokens below this are disqualified regardless of score — a 10% pool position
# on a $3.5K pool causes catastrophic slippage (getAmountsOut returns 0).
# Set below the $100K "meets_minimum" floor so both thresholds coexist:
#   $100K = pool exists and is listable
#   $5K  = pool can absorb a ~$350 position without price impact
MIN_EXEC_LIQUIDITY_USD = 5_000


def score_signal(
    token: dict,
    sr: dict,
    sm_netflow: dict,
    sm_trades: dict,
    top_holders: dict,
    pool: dict,
    btc_change_4h: float,
    list_type: str
) -> dict:
    """
    Score a token signal using 4 pillars (100 pts each, 400 total).
    
    Parameters:
        token: {'symbol': str, 'chain': str, 'address': str, 'min_liquidity_usd': float}
        sr: output of calculate_sr() - or None
        sm_netflow: output of get_sm_netflow()
        sm_trades: output of get_sm_dex_trades()
        top_holders: output of get_top_holders()
        pool: output of get_pool_liquidity()
        btc_change_4h: float - BTC % change last 4h (negative = down)
        list_type: 'core' or 'smart_money'
    
    Returns:
        dict with scoring results and disqualification info
    """
    
    # Hard disqualifiers - return score=0, passes=False immediately
    
    # Check pool meets minimum liquidity
    if not pool.get('meets_minimum', False):
        return _disqualified_result(token, list_type, "Pool does not meet minimum liquidity")

    # Hard floor: pool must be large enough to absorb a ~$350 position without
    # catastrophic price impact. A $3.5K pool × 10% = getAmountsOut returns 0.
    # $5K floor means max position is ~1% of pool — safe for all normal sizes.
    tvl = pool.get('tvl_usd', 0)
    if tvl > 0 and tvl < MIN_EXEC_LIQUIDITY_USD:
        return _disqualified_result(
            token, list_type,
            f"Pool liquidity ${tvl:.0f} below ${MIN_EXEC_LIQUIDITY_USD:,} execution floor"
        )
    
    # Check concentration risk
    if top_holders.get('concentration_risk', False):
        return _disqualified_result(token, list_type, "High concentration risk in top holders")
    
    # Check BTC change
    if btc_change_4h < -3.0:
        return _disqualified_result(token, list_type, "BTC down more than 3% in last 4h")
    
    # Check near resistance (only when SR data exists)
    if sr is not None and sr.get('near_resistance', False):
        return _disqualified_result(token, list_type, "Price near resistance level")
    
    # Check smart money netflow for smart_money list type
    # Multi-timeframe scoring: disqualify only if BOTH 24h and 7d are negative
    # Exception: sm_holdings tokens may have zero flow but 3+ SM wallets = conviction signal
    nf_1h = sm_netflow.get('net_flow_1h_usd', 0)
    nf_24h = sm_netflow.get('net_flow_24h_usd', sm_netflow.get('netflow_usd', 0))
    nf_7d = sm_netflow.get('net_flow_7d_usd', 0)

    # Prefer token's trader_count (from holdings discovery) over sm_netflow's cached value
    traders = token.get('trader_count', sm_netflow.get('trader_count', sm_netflow.get('sm_trader_count', 0)))
    is_holdings_conviction = traders >= 3  # SM wallets holding = strong signal even without flow

    if list_type == 'smart_money' and nf_24h <= 0 and nf_7d <= 0 and not is_holdings_conviction:
        return _disqualified_result(token, list_type, "Smart money netflow negative on both 24h and 7d")

    # Hard disqualifier: zero SM wallets — no wallets means no signal regardless of netflow
    if list_type == 'smart_money' and traders == 0:
        return _disqualified_result(token, list_type, "Zero SM wallets: no smart money holders")

    # No disqualifiers - calculate pillar scores
    
    # Pillar 1 - Price Structure (100 pts)
    # Missing SR: give partial credit (20 pts) if we have price data but no SR levels
    # (fresh SM tokens CEXs haven't adopted yet — not a fundamental failure)
    if sr is None:
        if token.get('price_usd', 0) > 0:
            pillar_price_structure = 20
        else:
            pillar_price_structure = 0
    else:
        pct = sr.get('pct_above_support', 100)
        if pct <= 2:
            pillar_price_structure = 100
        elif pct <= 5:
            pillar_price_structure = 75
        elif pct <= 10:
            pillar_price_structure = 40
        else:
            pillar_price_structure = 0
    
    # Pillar 2 - Smart Money Signal (100 pts)
    # Multi-timeframe gradient scoring based on netflow across timeframes
    # sm_holdings tokens with no flow data but 3+ SM wallets: partial credit based on conviction

    if list_type == 'smart_money':
        # Full flow signal: all timeframes positive
        if nf_1h > 0 and nf_24h > 0 and nf_7d > 0 and traders >= 10:
            pillar_smart_money = 100
        elif nf_24h > 0 and nf_7d > 0 and traders >= 5:
            pillar_smart_money = 75
        elif nf_24h > 0 and nf_7d > 0:
            pillar_smart_money = 60
        elif nf_24h > 0 and traders >= 5:
            pillar_smart_money = 40
        elif nf_24h > 0:
            pillar_smart_money = 20
        # Holdings conviction: no flow history but SM wallets are accumulating
        elif traders >= 10:
            pillar_smart_money = 40   # Static conviction: 10+ SM wallets
        elif traders >= 5:
            pillar_smart_money = 30   # Static conviction: 5-9 SM wallets
        elif traders >= 3:
            pillar_smart_money = 20   # Static conviction: 3-4 SM wallets
        else:
            pillar_smart_money = 0
    else:
        pillar_smart_money = 0  # Non-SM lists don't have SM signal data
    
    # Pillar 3 - Volume (100 pts)
    # DEX trades endpoint removed — use netflow as volume proxy when sm_trades is empty
    sm_buy_volume = sm_trades.get('sm_buy_volume_usd', 0)
    avg_daily_volume = sm_trades.get('avg_daily_volume_14d', 1)
    ratio = sm_buy_volume / max(avg_daily_volume, 1)
    
    if sm_buy_volume > 0 and avg_daily_volume > 0:
        # Full DEX volume data available
        if ratio >= 3.0:
            pillar_volume = 100
        elif ratio >= 2.0:
            pillar_volume = 70
        elif ratio >= 1.0:
            pillar_volume = 40
        else:
            pillar_volume = 0
    elif list_type == 'smart_money':
        # No DEX volume data — use netflow as proxy (smart money lists only)
        # Holdings conviction also qualifies: if 3+ SM wallets hold it, volume signal exists
        if nf_24h > 0 and traders >= 10:
            pillar_volume = 100
        elif nf_24h > 0 and traders >= 5:
            pillar_volume = 70
        elif nf_24h > 0:
            pillar_volume = 40
        elif traders >= 5:
            pillar_volume = 30   # Holdings conviction: 5+ SM wallets
        elif traders >= 3:
            pillar_volume = 20   # Holdings conviction: 3-4 SM wallets
        else:
            pillar_volume = 0
    else:
        # Non-SM lists: no volume signal available
        pillar_volume = 0
    
    # Pillar 4 - Token Health (100 pts)
    tvl = pool.get('tvl_usd', 0)
    pct5 = top_holders.get('top5_pct', 100)
    if tvl >= 1_000_000 and pct5 <= 30:
        pillar_token_health = 100
    elif tvl >= 500_000 and pct5 <= 40:
        pillar_token_health = 75
    elif tvl >= 100_000 and pct5 <= 50:
        pillar_token_health = 40
    else:
        pillar_token_health = 0
    
    # Calculate total score
    total_score = (
        pillar_price_structure +
        pillar_smart_money +
        pillar_volume +
        pillar_token_health
    )
    
    # Determine if passes threshold
    threshold = 175
    passes = total_score >= threshold
    
    # Build SR summary
    sr_summary = {
        'support': sr.get('support') if sr else None,
        'resistance': sr.get('resistance') if sr else None,
        'current_price': sr.get('current_price') if sr else token.get('price_usd'),
        'near_support': sr.get('near_support') if sr else None,
        'near_resistance': sr.get('near_resistance') if sr else None
    }
    
    return {
        'symbol': token.get('symbol'),
        'list_type': list_type,
        'total_score': total_score,
        'max_score': 400,
        'passes': passes,
        'threshold': threshold,
        'disqualified': False,
        'disqualify_reason': None,
        'pillars': {
            'price_structure': pillar_price_structure,
            'smart_money': pillar_smart_money,
            'volume': pillar_volume,
            'token_health': pillar_token_health
        },
        'sr_summary': sr_summary
    }


def _disqualified_result(token: dict, list_type: str, reason: str) -> dict:
    """
    Return a disqualified result with score=0.
    """
    return {
        'symbol': token.get('symbol'),
        'list_type': list_type,
        'total_score': 0,
        'max_score': 400,
        'passes': False,
        'threshold': 175,
        'disqualified': True,
        'disqualify_reason': reason,
        'pillars': {
            'price_structure': 0,
            'smart_money': 0,
            'volume': 0,
            'token_health': 0
        },
        'sr_summary': None
    }
