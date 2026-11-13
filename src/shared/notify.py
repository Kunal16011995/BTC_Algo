"""Simple notification dispatcher (Telegram + optional email)."""
from __future__ import annotations

import html
import json
import logging
import os
import random
import smtplib
import threading
import time
from email.mime.text import MIMEText
from pathlib import Path
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

# Auto-delete sent Telegram messages after this many seconds (default 24h).
TELEGRAM_MESSAGE_TTL_SECONDS = int(getattr(config, "TELEGRAM_MESSAGE_TTL_SECONDS", "86400"))

# Reuse TCP connections for Telegram; retry on transient network errors.
_telegram_session = requests.Session()

# Track Telegram message IDs so we can delete old notifications.
_MESSAGE_STORE_PATH = Path(getattr(config, "PROJECT_ROOT", ".")) / "data" / "telegram_message_ids.json"
_message_store_lock = threading.Lock()
_cleanup_started = False


def _load_message_store() -> list[dict]:
    if not _MESSAGE_STORE_PATH.exists():
        return []
    try:
        with open(_MESSAGE_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Could not load Telegram message store: %s", exc)
        return []


def _save_message_store(messages: list[dict]) -> None:
    try:
        _MESSAGE_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MESSAGE_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(messages, f, indent=2)
    except Exception as exc:
        log.warning("Could not save Telegram message store: %s", exc)


def _record_message_id(message_id: int) -> None:
    with _message_store_lock:
        messages = _load_message_store()
        messages.append({"id": message_id, "ts": time.time()})
        _save_message_store(messages)


def _delete_telegram_message(message_id: int) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id}
        resp = _telegram_session.post(url, json=payload, timeout=30)
        # Message may already be deleted; treat 400 "message to delete not found" as success.
        if resp.status_code == 200:
            return True
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("error_code") == 400 and "message to delete not found" in str(data.get("description", "")).lower():
            return True
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Could not delete Telegram message %s: %s", message_id, exc)
        return False


def _cleanup_old_messages() -> None:
    """Delete Telegram messages older than the configured TTL."""
    if TELEGRAM_MESSAGE_TTL_SECONDS <= 0:
        return
    cutoff = time.time() - TELEGRAM_MESSAGE_TTL_SECONDS
    with _message_store_lock:
        messages = _load_message_store()
        old = [m for m in messages if m.get("ts", 0) < cutoff]
        keep = [m for m in messages if m.get("ts", 0) >= cutoff]
        if not old:
            return
        deleted = 0
        for m in old:
            if _delete_telegram_message(m["id"]):
                deleted += 1
        log.info("Cleaned up %s/%s old Telegram notification messages", deleted, len(old))
        _save_message_store(keep)


def _cleanup_loop() -> None:
    """Run cleanup every hour."""
    while True:
        try:
            _cleanup_old_messages()
        except Exception as exc:
            log.warning("Telegram cleanup loop error: %s", exc)
        time.sleep(3600)


def _start_cleanup_thread() -> None:
    global _cleanup_started
    if _cleanup_started or TELEGRAM_MESSAGE_TTL_SECONDS <= 0:
        return
    _cleanup_started = True
    thread = threading.Thread(target=_cleanup_loop, daemon=True, name="telegram-cleanup")
    thread.start()
    log.info("Telegram message cleanup started (TTL=%ss)", TELEGRAM_MESSAGE_TTL_SECONDS)


def _noop(msg: str) -> None:
    pass


def _send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    _start_cleanup_thread()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram HTML parse mode rejects unescaped '<', '>' or '&'.  Escape the
    # whole text, then restore the explicit <b> tags we use for formatting.
    safe_msg = html.escape(msg)
    safe_msg = safe_msg.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": safe_msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last_exc: Exception | None = None
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            resp = _telegram_session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            message_id = data.get("result", {}).get("message_id")
            if message_id and TELEGRAM_MESSAGE_TTL_SECONDS > 0:
                _record_message_id(message_id)
            return True
        except Exception as exc:
            last_exc = exc
            log.warning("Telegram send attempt %s/%s failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                # Exponential backoff 2s, 4s, 8s, 16s plus small jitter.
                sleep_s = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(sleep_s)
    log.warning("Telegram notification failed after %s attempts: %s", max_retries, last_exc)
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


def notify_no_setup(
    variant: str,
    bar_ts,
    session: str,
    fvg_direction: str | None,
    leg_info: str,
    body_ratio: str,
    htf_aligned: bool,
    reasons: str,
) -> None:
    """Notify why the strategy did not produce a setup on the latest closed bar."""
    send(
        f"<b>No Setup — {variant}</b>\n"
        f"Bar: {bar_ts}\n"
        f"Session: {session}\n"
        f"FVG: {fvg_direction or 'none'}\n"
        f"Leg: {leg_info}\n"
        f"Body ratio: {body_ratio}\n"
        f"HTF: {'aligned' if htf_aligned else 'not_aligned'}\n"
        f"Reasons: {reasons}",
        subject=f"No setup {variant} @ {bar_ts}",
    )
