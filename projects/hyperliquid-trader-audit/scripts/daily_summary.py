#!/usr/bin/env python3
"""Daily email report generation for HyperLiquid trader."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import smtplib
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dateutil import tz

from reconcile import get_account_summary


DATA_DIR = Path(__file__).parent.parent / "data"
TRADE_LOG_FILE = DATA_DIR / "trade_log.jsonl"
SIGNAL_LOG_FILE = DATA_DIR / "signal_log.jsonl"
RUN_LOG_FILE = DATA_DIR / "run_log.jsonl"


def _now_et_iso() -> str:
    """Return current ET time as ISO string."""
    et_tz = tz.gettz("America/New_York")
    return datetime.now(et_tz).isoformat()


def _format_date_et() -> str:
    """Return current date in ET as YYYY-MM-DD."""
    et_tz = tz.gettz("America/New_York")
    return datetime.now(et_tz).strftime("%Y-%m-%d")


def _format_currency(value: float) -> str:
    """Format dollar amount with $ sign and 2 decimal places."""
    return f"${value:,.2f}"


def _format_pct(value: float) -> str:
    """Format percentage with 1 decimal place and +/- sign."""
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _filter_today_log(log_file: Path) -> list:
    """Filter log entries for today (ET date)."""
    if not log_file.exists():
        return []
    
    today = _format_date_et()
    entries = []
    
    with open(log_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry_date = entry.get("timestamp", "").split("T")[0]
                if entry_date == today:
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    
    return entries


def _get_run_stats() -> dict:
    """Get pipeline health stats from run_log.jsonl."""
    stats = {
        "last_run": None,
        "run_count_today": 0,
        "error_count_today": 0
    }
    
    if not RUN_LOG_FILE.exists():
        return stats
    
    today = _format_date_et()
    
    with open(RUN_LOG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry_date = entry.get("timestamp", "").split("T")[0]
                if entry_date == today:
                    stats["run_count_today"] += 1
                    if entry.get("status") == "error":
                        stats["error_count_today"] += 1
                    if stats["last_run"] is None or entry.get("timestamp", "") > stats["last_run"]:
                        stats["last_run"] = entry.get("timestamp")
            except json.JSONDecodeError:
                continue
    
    return stats


def _generate_positions_table(positions: list, funding_rates: dict) -> str:
    """Generate HTML table for open positions.
    
    Direction: LONG (green) or SHORT (red) with color coding.
    """
    if not positions:
        return "<p>No open positions.</p>"
    
    rows = []
    for pos in positions:
        symbol = pos.get("asset", "")
        direction = pos.get("direction", "").upper()
        entry_price = float(pos.get("entry_price", 0))
        current_price = float(pos.get("current_price", 0))
        unrealized_pnl = float(pos.get("unrealized_pnl", 0))
        leverage = pos.get("leverage", 1)
        opened_at = pos.get("opened_at_utc", "")
        stop_loss = pos.get("stop_loss", 0)
        take_profit = pos.get("take_profit", 0)
        
        # Calculate time open
        try:
            opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            now_et = datetime.now(tz.gettz("America/New_York"))
            time_open = (now_et - opened_dt.replace(tzinfo=None)).total_seconds() / 3600
            time_open_str = f"{time_open:.1f}h"
        except:
            time_open_str = "N/A"
        
        # Color direction: LONG = green, SHORT = red
        direction_color = "green" if direction == "LONG" else "red"
        
        # Color PnL
        pnl_color = "green" if unrealized_pnl >= 0 else "red"
        
        # Get funding rate
        funding = funding_rates.get(symbol, 0)
        funding_color = "green" if funding > 0 else "red"
        
        rows.append(f"""
        <tr>
            <td style="border: 1px solid #ddd; padding: 6px;">{symbol}</td>
            <td style="border: 1px solid #ddd; padding: 6px; color: {direction_color}; font-weight: bold;">{direction}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{entry_price:.4f}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{current_price:.4f}</td>
            <td style="border: 1px solid #ddd; padding: 6px; color: {pnl_color}; font-weight: bold;">{unrealized_pnl:+.2f}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{leverage}x</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{time_open_str}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{stop_loss:.4f}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{take_profit:.4f}</td>
            <td style="border: 1px solid #ddd; padding: 6px; color: {funding_color};">{funding:+.6f}</td>
        </tr>
        """)
    
    return """
    <table style="border-collapse: collapse; width: 100%; font-size: 12px;">
        <thead>
            <tr style="background-color: #f5f5f5;">
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Symbol</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Direction</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Entry</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Current</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Unrealized PnL</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: center;">Leverage</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: center;">Time Open</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Stop Loss</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Take Profit</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Funding</th>
            </tr>
        </thead>
        <tbody>
            """ + "".join(rows) + """
        </tbody>
    </table>
    """


def _generate_trades_table(trades: list) -> str:
    """Generate HTML table for today's trades."""
    if not trades:
        return "<p>No trades today.</p>"
    
    rows = []
    for trade in trades:
        symbol = trade.get("symbol", "")
        direction = trade.get("direction", "").upper()
        action = trade.get("action", "").upper()
        price = float(trade.get("price", 0))
        size = trade.get("size", 0)
        reason = trade.get("reason", "")
        
        direction_color = "green" if direction == "LONG" else "red"
        
        rows.append(f"""
        <tr>
            <td style="border: 1px solid #ddd; padding: 6px;">{symbol}</td>
            <td style="border: 1px solid #ddd; padding: 6px; color: {direction_color}; font-weight: bold;">{direction}</td>
            <td style="border: 1px solid #ddd; padding: 6px; font-weight: bold;">{action}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{price:.4f}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{size}</td>
            <td style="border: 1px solid #ddd; padding: 6px;">{reason}</td>
        </tr>
        """)
    
    return """
    <table style="border-collapse: collapse; width: 100%; font-size: 12px;">
        <thead>
            <tr style="background-color: #f5f5f5;">
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Symbol</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Direction</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Action</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Price</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: center;">Size</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Reason</th>
            </tr>
        </thead>
        <tbody>
            """ + "".join(rows) + """
        </tbody>
    </table>
    """


def _generate_signals_table(signals: list) -> str:
    """Generate HTML table for today's top 5 signals."""
    if not signals:
        return "<p>No signals today.</p>"
    
    # Sort by score descending, take top 5
    sorted_signals = sorted(signals, key=lambda x: float(x.get("score", 0)), reverse=True)[:5]
    
    rows = []
    for signal in sorted_signals:
        symbol = signal.get("symbol", "")
        score = float(signal.get("score", 0))
        direction = signal.get("direction", "").upper()
        entry_confirmed = "Y" if signal.get("entry_confirmed", False) else "N"
        
        direction_color = "green" if direction == "LONG" else "red"
        
        rows.append(f"""
        <tr>
            <td style="border: 1px solid #ddd; padding: 6px;">{symbol}</td>
            <td style="border: 1px solid #ddd; padding: 6px; font-weight: bold;">{score:.1f}</td>
            <td style="border: 1px solid #ddd; padding: 6px; color: {direction_color}; font-weight: bold;">{direction}</td>
            <td style="border: 1px solid #ddd; padding: 6px; text-align: center;">{entry_confirmed}</td>
        </tr>
        """)
    
    return """
    <table style="border-collapse: collapse; width: 100%; font-size: 12px;">
        <thead>
            <tr style="background-color: #f5f5f5;">
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Symbol</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Score</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Direction</th>
                <th style="border: 1px solid #ddd; padding: 6px; text-align: center;">Entry</th>
            </tr>
        </thead>
        <tbody>
            """ + "".join(rows) + """
        </tbody>
    </table>
    """


def generate_report(config: dict) -> str:
    """
    Generate an HTML email with inline styles containing daily trading summary.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        HTML string for email body
    """
    today = _format_date_et()
    
    # Get account summary
    account_summary = get_account_summary(config)
    
    # Get open positions
    from positions import get_open_positions
    positions = get_open_positions()
    
    # Get funding rates (mock - in real impl would fetch from API)
    funding_rates = {}
    for pos in positions:
        symbol = pos.get("asset", "")
        # Mock funding rate for demo
        funding_rates[symbol] = 0.0001 if pos.get("direction", "").lower() == "long" else -0.0001
    
    # Get today's trades
    trades = _filter_today_log(TRADE_LOG_FILE)
    
    # Get today's signals
    signals = _filter_today_log(SIGNAL_LOG_FILE)
    
    # Get pipeline health
    run_stats = _get_run_stats()
    
    # Build HTML with inline styles only (no CSS blocks - Gmail strips them)
    html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
    </head>
    <body style="font-family: Arial, sans-serif; margin: 20px; background-color: #f9f9f9;">
        
        <!-- HEADER -->
        <div style="background-color: #2c3e50; color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px;">
            <h1 style="margin: 0; font-size: 24px;">HyperLiquid Trader Daily Report - {today}</h1>
        </div>
        
        <!-- ACCOUNT SUMMARY -->
        <div style="background-color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color: #2c3e50; margin-top: 0;">Account Summary</h2>
            <table style="width: 100%; font-size: 14px;">
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Account Value:</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: right; font-weight: bold;">{_format_currency(account_summary.get('account_value', 0))}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Margin Used:</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: right;">{_format_currency(account_summary.get('total_margin_used', 0))}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Withdrawable:</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: right; font-weight: bold;">{_format_currency(account_summary.get('withdrawable', 0))}</td>
                </tr>
                <tr>
                    <td style="padding: 8px;"><strong>Total Unrealized PnL:</strong></td>
                    <td style="padding: 8px; text-align: right; font-weight: bold; color: {'green' if account_summary.get('total_unrealized_pnl', 0) >= 0 else 'red'};">
                        {_format_currency(account_summary.get('total_unrealized_pnl', 0))}
                    </td>
                </tr>
            </table>
        </div>
        
        <!-- OPEN POSITIONS -->
        <div style="background-color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color: #2c3e50; margin-top: 0;">Open Positions ({len(positions)})</h2>
            {_generate_positions_table(positions, funding_rates)}
        </div>
        
        <!-- TODAY'S TRADES -->
        <div style="background-color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color: #2c3e50; margin-top: 0;">Today's Trades ({len(trades)})</h2>
            {_generate_trades_table(trades)}
        </div>
        
        <!-- TODAY'S SIGNALS -->
        <div style="background-color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color: #2c3e50; margin-top: 0;">Today's Top Signals ({len(signals)})</h2>
            {_generate_signals_table(signals)}
        </div>
        
        <!-- PIPELINE HEALTH -->
        <div style="background-color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color: #2c3e50; margin-top: 0;">Pipeline Health</h2>
            <table style="width: 100%; font-size: 14px;">
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Last Run:</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{run_stats.get('last_run', 'N/A')}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;"><strong>Runs Today:</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{run_stats.get('run_count_today', 0)}</td>
                </tr>
                <tr>
                    <td style="padding: 8px;"><strong>Errors Today:</strong></td>
                    <td style="padding: 8px; color: {'red' if run_stats.get('error_count_today', 0) > 0 else 'green'}; font-weight: bold;">
                        {run_stats.get('error_count_today', 0)}
                    </td>
                </tr>
            </table>
        </div>
        
        <!-- FUNDING RATES -->
        <div style="background-color: white; padding: 20px; border-radius: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <h2 style="color: #2c3e50; margin-top: 0;">Funding Rates</h2>
            <table style="width: 100%; font-size: 12px;">
                <thead>
                    <tr style="background-color: #f5f5f5;">
                        <th style="border: 1px solid #ddd; padding: 6px; text-align: left;">Symbol</th>
                        <th style="border: 1px solid #ddd; padding: 6px; text-align: right;">Funding Rate</th>
                    </tr>
                </thead>
                <tbody>
    """
    
    for symbol, funding in funding_rates.items():
        funding_color = "green" if funding > 0 else "red"
        html += f"""
                    <tr>
                        <td style="border: 1px solid #ddd; padding: 6px;">{symbol}</td>
                        <td style="border: 1px solid #ddd; padding: 6px; color: {funding_color}; font-weight: bold;">{funding:+.6f}</td>
                    </tr>
        """
    
    html += """
                </tbody>
            </table>
        </div>
        
    </body>
    </html>
    """
    
    return html


def send_email(subject: str, html_body: str, config: dict) -> bool:
    """
    Send email using smtplib with env vars.
    
    Args:
        subject: Email subject
        html_body: HTML body content
        config: Configuration dictionary
        
    Returns:
        True on success, False on failure
    """
    try:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASS")
        email_to = os.environ.get("EMAIL_TO")
        
        if not all([smtp_host, smtp_user, smtp_pass, email_to]):
            print("Missing required email environment variables")
            return False
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = email_to
        
        # Attach HTML body as multipart/alternative
        html_part = MIMEText(html_body, "html")
        msg.attach(html_part)
        
        # Connect and send
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        
        return True
        
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def _send_telegram_summary(subject: str, html_body: str, config: dict) -> bool:
    """Send condensed 5-line Telegram summary."""
    try:
        telegram_token = os.environ.get("TELEGRAM_TOKEN")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        
        if not all([telegram_token, telegram_chat_id]):
            print("Missing Telegram environment variables")
            return False
        
        # Parse HTML to extract key info
        today = _format_date_et()
        account_value = html_body.split("Account Value:")[1].split("</td>")[0].strip() if "Account Value:" in html_body else "N/A"
        unrealized_pnl = html_body.split("Total Unrealized PnL:")[1].split("</td>")[0].strip() if "Total Unrealized PnL:" in html_body else "N/A"
        
        # Count positions and trades
        pos_count = html_body.count("<tr>") // 10 if html_body.count("<tr>") > 0 else 0
        
        summary = f"""
_ HL Trader Daily - {today}
_ Account: {account_value}
_ PnL: {unrealized_pnl}
_ Positions: {pos_count}
_ Subject: {subject}
""".strip()
        
        import requests
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        data = {
            "chat_id": telegram_chat_id,
            "text": summary
        }
        
        response = requests.post(url, json=data)
        return response.status_code == 200
        
    except Exception as e:
        print(f"Failed to send Telegram: {e}")
        return False


def main():
    """Main entry point for daily summary."""
    import yaml
    
    # Load config
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    
    # Generate report
    html_body = generate_report(config)
    
    # Get stats for subject
    account_summary = get_account_summary(config)
    positions = [p for p in get_open_positions()]
    total_pnl = account_summary.get("total_unrealized_pnl", 0)
    open_count = len(positions)
    
    subject = f"HL Trader Daily - {_format_date_et()} | {open_count} positions | PnL: {_format_currency(total_pnl)}"
    
    # Send email
    email_sent = send_email(subject, html_body, config)
    print(f"Email sent: {email_sent}")
    
    # Send Telegram summary
    telegram_sent = _send_telegram_summary(subject, html_body, config)
    print(f"Telegram sent: {telegram_sent}")


if __name__ == "__main__":
    main()