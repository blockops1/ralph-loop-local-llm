"""
cost_check.py — Pre-buy cost estimation for Solana trader.

Estimates slippage + fees before committing to a buy.
Uses Jupiter Ultra API quote to get expected output, compares to market price.

Returns:
    dict with: passes (bool), slippage_pct, fee_sol, fee_pct, total_cost_pct, reason
"""
import os, json, requests
from pathlib import Path
from datetime import datetime, timezone

# Fixed Solana network fee per transaction (lamports → SOL)
# Jupiter Ultra tx is ~5000-15000 lamports; use conservative 0.0005 SOL
SOLANA_FEE_SOL = 0.0005
WSOL = 'So11111111111111111111111111111111111111112'


def _load_env():
    env_file = Path.home() / '.hermes' / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                k = k.strip().removeprefix('export').strip()
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))

_load_env()


def get_jupiter_quote(mint: str, amount_lamports: int) -> dict | None:
    """Get Jupiter quote for SOL → token swap. Returns quote dict or None on error."""
    try:
        r = requests.get(
            'https://quote-api.jup.ag/v6/quote',
            params={
                'inputMint': WSOL,
                'outputMint': mint,
                'amount': amount_lamports,
                'slippageBps': 50,   # 0.5% — same as Ultra API default
            },
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'[COST_CHECK] Jupiter quote error: {e}')
    return None


def estimate_cost(mint: str, symbol: str, amount_sol: float, sol_price_usd: float,
                  market_price_sol: float, position_usd: float) -> dict:
    """
    Estimate slippage + fees as % of position before executing buy.

    Args:
        mint: Token mint address
        symbol: Token symbol (for logging)
        amount_sol: SOL amount to spend
        sol_price_usd: Current SOL/USD price
        market_price_sol: Current market price of token in SOL (from DexScreener)
        position_usd: Position size in USD

    Returns:
        dict: {passes, slippage_pct, fee_sol, fee_pct, total_cost_pct, expected_tokens, reason}
    """
    result = {
        'passes': True,
        'slippage_pct': 0.0,
        'fee_sol': SOLANA_FEE_SOL,
        'fee_pct': 0.0,
        'total_cost_pct': 0.0,
        'expected_tokens': 0,
        'reason': ''
    }

    try:
        amount_lamports = int(amount_sol * 1e9)
        quote = get_jupiter_quote(mint, amount_lamports)

        if quote:
            # Expected tokens from Jupiter (after Jupiter's routing + slippage)
            out_amount = int(quote.get('outAmount', 0))
            result['expected_tokens'] = out_amount

            # Market price implies how many tokens we'd get with zero slippage
            if market_price_sol > 0:
                ideal_tokens_raw = amount_lamports / (market_price_sol * 1e9)
                # out_amount is raw integer; compare proportionally
                # We don't know decimals here, but ratio is valid
                if ideal_tokens_raw > 0:
                    slippage_ratio = 1 - (out_amount / (ideal_tokens_raw * 1e9 / 1e9 * 1e9 / 1e9))
                    # Simpler: use priceImpactPct from Jupiter directly if available
                    price_impact = float(quote.get('priceImpactPct', 0) or 0)
                    result['slippage_pct'] = abs(price_impact)
        else:
            # No quote available — use a conservative default
            result['slippage_pct'] = 1.0
            result['reason'] = 'no_quote_available'

        # Fee as % of position
        fee_usd = SOLANA_FEE_SOL * sol_price_usd
        result['fee_pct'] = (fee_usd / position_usd * 100) if position_usd > 0 else 0.0
        result['fee_sol'] = SOLANA_FEE_SOL
        result['total_cost_pct'] = result['slippage_pct'] + result['fee_pct']

    except Exception as e:
        print(f'[COST_CHECK] Estimation error for {symbol}: {e}')
        result['reason'] = f'estimation_error: {e}'
        result['total_cost_pct'] = 0.0  # Don't block on estimation error

    return result


def check_cost_threshold(mint: str, symbol: str, amount_sol: float, sol_price_usd: float,
                         market_price_sol: float, position_usd: float,
                         max_cost_pct: float) -> dict:
    """
    Full cost check with pass/fail decision.

    Returns dict with all cost fields plus:
        passes: True if cost within threshold (or threshold disabled)
        skip_reason: human-readable reason if blocked
    """
    if max_cost_pct <= 0:
        return {'passes': True, 'total_cost_pct': 0.0, 'reason': 'check_disabled'}

    est = estimate_cost(mint, symbol, amount_sol, sol_price_usd, market_price_sol, position_usd)

    if est['total_cost_pct'] > max_cost_pct:
        est['passes'] = False
        est['reason'] = (f'cost_too_high: {est["total_cost_pct"]:.2f}% > threshold {max_cost_pct:.1f}% '
                         f'(slippage {est["slippage_pct"]:.2f}% + fees {est["fee_pct"]:.2f}%)')
        print(f'[COST_CHECK] {symbol}: SKIP — {est["reason"]}')
    else:
        est['passes'] = True
        print(f'[COST_CHECK] {symbol}: OK — cost {est["total_cost_pct"]:.2f}% ≤ {max_cost_pct:.1f}%')

    est['threshold'] = max_cost_pct
    return est


def log_skipped(data_dir: Path, symbol: str, address: str, score: int,
                position_usd: float, cost_est: dict):
    """Append a cost-skipped entry to skipped_log.jsonl."""
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'reason': cost_est.get('reason', 'cost_too_high'),
        'symbol': symbol,
        'address': address,
        'score': score,
        'position_usd': position_usd,
        'est_slippage_pct': round(cost_est.get('slippage_pct', 0), 3),
        'est_fee_pct': round(cost_est.get('fee_pct', 0), 3),
        'total_cost_pct': round(cost_est.get('total_cost_pct', 0), 3),
        'threshold': cost_est.get('threshold', 0),
    }
    log_path = data_dir / 'skipped_log.jsonl'
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')
    print(f'[COST_CHECK] {symbol}: logged to skipped_log.jsonl')
