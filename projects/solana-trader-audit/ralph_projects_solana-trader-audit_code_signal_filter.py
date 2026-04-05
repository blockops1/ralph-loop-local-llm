def score_signal(token: dict, flow_intel: dict, btc_change_4h: float, config: dict) -> dict:
    """
    Score a Solana token for entry.

    token: from load_candidate_tokens() - has symbol, address, netflow_usd, liquidity_usd, market_cap_usd, volume_usd
    flow_intel: from get_flow_intelligence() - has smart_trader_net_flow_usd, smart_trader_wallet_count,
                whale_net_flow_usd, top_pnl_net_flow_usd, etc.
    btc_change_4h: float
    config: loaded config.yaml dict

    Returns dict:
      passes: bool
      total_score: int
      disqualified_reason: str or None
      pillars: dict of per-pillar scores
      symbol: str
      address: str
    """

    def disq(reason):
        return {'passes': False, 'total_score': 0, 'disqualified_reason': reason,
                'pillars': {}, 'symbol': token['symbol'], 'address': token['address']}

    # Hard disqualifiers
    if btc_change_4h < config.get('btc_kill_switch_pct', -3.0):
        return disq('BTC kill switch: 4h drop > 3%')

    smart_trader_wallets = flow_intel.get('trader_count', 0) or 0
    if smart_trader_wallets == 0:
        return disq('Zero smart trader wallets - no SM signal')

    netflow = flow_intel.get('net_flow_24h_usd', 0) or 0
    if netflow <= 0:
        return disq('Negative or zero SM netflow')

    # Pillar 1: SM Netflow (100 pts)
    if netflow > 500000:
        p1 = 100
    elif netflow > 100000:
        p1 = 75
    elif netflow > 25000:
        p1 = 50
    elif netflow > 0:
        p1 = 25
    else:
        p1 = 0

    # Pillar 2: Flow Intelligence (100 pts)
    p2 = 0
    smart_nf = flow_intel.get('net_flow_24h_usd', 0) or 0
    whale_nf = flow_intel.get('whale_net_flow_usd', 0) or 0
    pnl_nf = flow_intel.get('top_pnl_net_flow_usd', 0) or 0
    if smart_nf > 0 and smart_trader_wallets >= 3:
        p2 += 40
    elif smart_nf > 0:
        p2 += 20
    if whale_nf > 0:
        p2 += 35
    if pnl_nf > 0:
        p2 += 25

    # Pillar 3: Volume (100 pts)
    vol = token.get('volume_24h_usd', 0) or 0
    if vol > 10000000:
        p3 = 100
    elif vol > 1000000:
        p3 = 75
    elif vol > 100000:
        p3 = 50
    elif vol > 10000:
        p3 = 25
    else:
        p3 = 0

    # Pillar 4: Token Health (100 pts) - liquidity + age
    liq = token.get('liquidity_usd', 0) or 0
    age = token.get('token_age_days', 0) or 0
    p4 = 0
    if liq > 1000000:
        p4 += 50
    elif liq > 500000:
        p4 += 35
    elif liq > 100000:
        p4 += 20
    if age > 365:
        p4 += 50
    elif age > 180:
        p4 += 35
    elif age > 30:
        p4 += 20

    total = p1 + p2 + p3 + p4
    threshold = config.get('entry_score_threshold', 175)
    passes = total >= threshold

    return {
        'passes': passes,
        'total_score': total,
        'disqualified_reason': None if passes else f'Score {total} below threshold {threshold}',
        'pillars': {
            'sm_netflow': p1,
            'flow_intelligence': p2,
            'volume': p3,
            'token_health': p4,
            'smart_trader_wallets': smart_trader_wallets,
            'smart_trader_nf_usd': smart_nf,
            'whale_nf_usd': whale_nf,
            'pnl_nf_usd': pnl_nf,
        },
        'symbol': token['symbol'],
        'address': token['address'],
        'token': token,
    }
