"""Simple notification dispatcher (Telegram + optional email)."""
from __future__ import annotations

import logging
import smtplib
import time
from email.mime.text import MIMEText
from typing import Callable

import requests

from src.shared import config

log = logging.getLogger("notify")

TELEGRAM_BOT_TOKEN = getattr(config, "TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = getattr(config, "TELEGRAM_CHAT_ID", "")
EMAIL_SMTP_HOST = getattr(config, "EMAIL_SMTP_HOST", "")
EMAIL_SMTP_PORT = int(getattr(config, "EMAIL_SMTP_PORT", "587"))
EMAIL_SMTP_USER = getattr(config, "EMAIL_SMTP_USER", "")
EMAIL_SMTP_PASSWORD = getattr(config, "EMAIL_SMTP_PASSWORD", "")
EMAIL_FROM = getattr(config, "EMAIL_FROM", "")
EMAIL_TO = getattr(config, "EMAIL_TO", "")

# Reuse TCP connections for Telegram; retry on transient network errors.
_telegram_session = requests.Session()


def _noop(msg: str) -> None:
    pass


def _send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = _telegram_session.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            return True
        except Exception as exc:
            last_exc = exc
            log.warning("Telegram send attempt %s/3 failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 ** attempt)  # 2s, 4s backoff
    log.warning("Telegram notification failed after 3 attempts: %s", last_exc)
    return False


def _send_email(msg: str, subject: str = "BTC FVG Bot Alert") -> bool:
    if not all([EMAIL_SMTP_HOST, EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO]):
        return False
    try:
        mime = MIMEText(msg)
        mime["Subject"] = subject
        mime["From"] = EMAIL_FROM
        mime["To"] = EMAIL_TO
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(EMAIL_SMTP_USER, EMAIL_SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], mime.as_string())
        return True
    except Exception as exc:
        log.warning("Email notification failed: %s", exc)
        return False


def _dispatchers() -> list[Callable[[str], bool]]:
    dispatchers: list[Callable[[str], bool]] = []
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        dispatchers.append(_send_telegram)
    if EMAIL_SMTP_HOST and EMAIL_SMTP_USER and EMAIL_SMTP_PASSWORD and EMAIL_TO:
        # email signature is (msg, subject), wrapped below
        dispatchers.append(lambda msg: _send_email(msg))
    return dispatchers


def send(msg: str, subject: str = "BTC FVG Bot Alert") -> bool:
    """Send a notification via all configured channels. Returns True if at least one channel succeeded."""
    success = False
    for fn in _dispatchers():
        try:
            result = fn(msg)
            if result:
                success = True
        except Exception as exc:
            log.warning("Notification channel failed: %s", exc)
    return success


def notify_entry(
    variant: str,
    side: str,
    qty: float,
    entry_price: float,
    stop_price: float,
    tp1_price: float,
    runner_max_price: float,
    live: bool = True,
) -> None:
    channel = "LIVE" if live else "PAPER"
    send(
        f"<b>{channel} Entry — {variant}</b>\n"
        f"Side: {side}\n"
        f"Qty: {qty:.3f} BTC\n"
        f"Entry: {entry_price:,.2f}\n"
        f"SL: {stop_price:,.2f}\n"
        f"TP1: {tp1_price:,.2f}\n"
        f"Runner max: {runner_max_price:,.2f}",
        subject=f"{channel} Entry {side} @ {entry_price:,.2f}",
    )


def notify_sl_trail(
    variant: str,
    old_sl: float,
    new_sl: float,
    extreme: float,
    bar_ts,
    live: bool = True,
) -> None:
    channel = "LIVE" if live else "PAPER"
    send(
        f"<b>{channel} SL Trailed — {variant}</b>\n"
        f"Old SL: {old_sl:,.2f}\n"
        f"New SL: {new_sl:,.2f}\n"
        f"Extreme: {extreme:,.2f}\n"
        f"Bar: {bar_ts}",
        subject=f"{channel} SL Trailed {old_sl:,.2f} → {new_sl:,.2f}",
    )


def notify_exit(
    variant: str,
    side: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    pnl: float,
    reason: str,
    equity: float,
    live: bool = True,
) -> None:
    channel = "LIVE" if live else "PAPER"
    emoji = "🟢" if pnl >= 0 else "🔴"
    send(
        f"<b>{channel} Exit — {variant} {emoji}</b>\n"
        f"Reason: {reason}\n"
        f"Side: {side}\n"
        f"Qty: {qty:.3f} BTC\n"
        f"Entry: {entry_price:,.2f}\n"
        f"Exit: {exit_price:,.2f}\n"
        f"PnL: ${pnl:,.2f}\n"
        f"Equity: ${equity:,.2f}",
        subject=f"{channel} Exit {reason} | PnL ${pnl:,.2f}",
    )
