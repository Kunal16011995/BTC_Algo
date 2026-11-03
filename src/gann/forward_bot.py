"""
Live forward-test bot for the WD Gann Overnight strategy.

- Trades BTCUSDT daily on a dedicated Bybit demo account (GANN_BYBIT_API_*).
- Uses effective_capital = min(allocated, Bybit account equity).
- Uses the same signal engine as gann_overnight_paper.py:
    * weekly trend >= 1.0%
    * daily reversal >= 1.5%
    * trailing stop 0.5% beyond latest higher bottom / lower top
- Variant name stored in bot.db: 'gann_forward'.
"""

from __future__ import annotations

import http.server
import json
import logging
import socketserver
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared import store
from src.shared.bybit_client import BybitClient
from src.gann.strategy import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    latest_higher_bottom,
    latest_lower_top,
    prepare_dataframe,
    signal_for_day,
    stop_price_for_direction,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SYMBOL = "BTCUSDT"
INTERVAL = "D"
ALLOCATED_CAPITAL = 100_000.0
LEVERAGE = 1.5
BASE_UNIT_NOTIONAL_PCT = 0.75
STOP_LOSS_PCT = 0.5
DAILY_REVERSAL_THRESHOLD_PCT = 1.5
WEEKLY_REVERSAL_THRESHOLD_PCT = 1.0
DIRECTION = "both"

# Bybit fees for BTCUSDT linear perp (taker ~0.055%) + slippage assumptions
TRADING_FEE_PCT = 0.055
SLIPPAGE_PCT = 0.03
STOP_SLIPPAGE_PCT = 0.05
ENTRY_COST_PCT = TRADING_FEE_PCT + SLIPPAGE_PCT
EXIT_COST_PCT = TRADING_FEE_PCT + SLIPPAGE_PCT
STOP_EXIT_COST_PCT = TRADING_FEE_PCT + STOP_SLIPPAGE_PCT

HEALTH_PORT = 5558
VARIANT = "gann_forward"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(config.PROJECT_ROOT / "logs" / "gann_forward_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("gann_forward_bot")

client = BybitClient(
    config.GANN_BYBIT_API_KEY,
    config.GANN_BYBIT_API_SECRET,
    config.GANN_BYBIT_DEMO_BASE_URL,
    config.GANN_BYBIT_PUBLIC_BASE_URL,
)

_state = {"running": False, "last_processed_day": None, "error_count": 0, "instr": None}
OPEN_TRADES_KEY = "open_gann_forward_trades"


# ---------------------------------------------------------------------------
# Bybit helpers
# ---------------------------------------------------------------------------
def _get_instrument_info():
    info = client.get_instrument(SYMBOL)
    lot = info.get("lotSizeFilter", {})
    price = info.get("priceFilter", {})
    qty_step = float(lot.get("qtyStep", "0.001") or "0.001")
    min_qty = float(lot.get("minOrderQty", "0.001") or "0.001")
    tick_size = float(price.get("tickSize", "0.1") or "0.1")
    return {"qty_step": qty_step, "min_qty": min_qty, "tick_size": tick_size}


def _round_size(size: float, size_step: float, min_size: float) -> float:
    rounded = max(min_size, round(size / size_step) * size_step)
    return rounded


def _round_price(price: float, tick_size: float) -> float:
    return round(price / tick_size) * tick_size


def _position_side_to_int(side: str) -> int:
    return DIRECTION_LONG if side == "Buy" else DIRECTION_SHORT


def _int_to_side(direction: int) -> str:
    return "Buy" if direction == DIRECTION_LONG else "Sell"


def _get_position() -> dict | None:
    pos = client.get_position(SYMBOL)
    if not pos:
        return None
    size = float(pos.get("size", 0) or 0)
    if abs(size) == 0:
        return None
    return pos


def _get_wallet_equity() -> float:
    try:
        balance = client.get_wallet_balance()
        return float(balance.get("totalEquity", 0) or 0)
    except Exception as exc:
        log.warning("Could not read wallet equity: %s", exc)
        return 0.0


def _effective_capital() -> float:
    """Use the Bybit wallet balance if it is lower than the theoretical allocation."""
    wallet = _get_wallet_equity()
    if wallet > 0:
        return min(ALLOCATED_CAPITAL, wallet)
    return ALLOCATED_CAPITAL


def _load_open_gann_trades() -> list[dict]:
    raw = store.get_meta(OPEN_TRADES_KEY, "")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_open_gann_trades(trades: list[dict]):
    store.set_meta(OPEN_TRADES_KEY, json.dumps(trades, default=str))


def _match_closed_row_to_gann(row: dict, open_trades: list[dict]) -> dict | None:
    """Match a Bybit closed-PnL row to one of our tracked Gann trades."""
    try:
        close_side = row.get("side", "")
        qty = float(row.get("qty", 0) or 0)
        entry_price = float(row.get("avgEntryPrice", 0) or 0)
        updated_ms = int(row.get("updatedTime", 0) or 0)
    except Exception:
        return None

    for trade in open_trades:
        if trade.get("side") == "Buy" and close_side != "Sell":
            continue
        if trade.get("side") == "Sell" and close_side != "Buy":
            continue
        qty_diff = abs(float(trade.get("qty", 0) or 0) - qty)
        if qty_diff > 0.01:
            continue
        entry = float(trade.get("entry_price", 0) or 0)
        entry_tol = max(entry, entry_price) * 0.02
        if entry > 0 and entry_price > 0 and abs(entry - entry_price) > entry_tol:
            continue
        if updated_ms < int(trade.get("entry_ts_ms", 0) or 0):
            continue
        return trade
    return None


def _fetch_daily_klines(limit=200) -> pd.DataFrame:
    rows = client.get_klines(SYMBOL, INTERVAL, limit=limit)
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "turnover"])
    df["timestamp"] = pd.to_datetime(df["open_time"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
def _base_notional(equity: float) -> float:
    return equity * BASE_UNIT_NOTIONAL_PCT


def _compute_entry_size(close: float, equity: float, instr: dict) -> float | None:
    """Return the BTCUSDT quantity for the desired notional position."""
    notional = _base_notional(equity)
    qty = notional / close
    qty = _round_size(qty, instr["qty_step"], instr["min_qty"])
    margin = notional / LEVERAGE
    if margin + notional * ENTRY_COST_PCT / 100 > equity:
        return None
    return qty


# ---------------------------------------------------------------------------
# PnL / recording
# ---------------------------------------------------------------------------
def _record_closed_trade():
    """Pull closed-PnL, match rows to our tracked Gann trades, and record them."""
    open_trades = _load_open_gann_trades()
    if not open_trades:
        return

    try:
        rows = client.get_all_closed_pnl(SYMBOL)
    except Exception as exc:
        log.warning("Could not fetch closed PnL: %s", exc)
        return

    existing = store.load_trades_variant(VARIANT)
    recorded = 0
    for row in rows:
        key = f"{row.get('orderId', '')}-{row.get('updatedTime', '')}"
        if any(t["id"] == key for t in existing):
            continue

        trade = _match_closed_row_to_gann(row, open_trades)
        if trade is None:
            continue

        qty = float(row.get("qty", 0) or 0)
        entry = float(row.get("avgEntryPrice", 0) or 0)
        exit_ = float(row.get("avgExitPrice", 0) or 0)
        pnl = float(row.get("closedPnl", 0) or 0)
        if qty <= 0 or entry <= 0 or exit_ <= 0:
            continue

        store.upsert_trade_variant(
            {
                "id": key,
                "variant": VARIANT,
                "symbol": SYMBOL,
                "side": trade.get("side", ""),
                "qty": qty,
                "entry_price": entry,
                "exit_price": exit_,
                "pnl": pnl,
                "exit_ts": int(row.get("updatedTime", 0) or 0),
                "raw": row,
            }
        )
        log.info(
            "Recorded closed Gann forward trade | side=%s qty=%.3f entry=%.2f exit=%.2f pnl=%.2f",
            trade.get("side", ""), qty, entry, exit_, pnl,
        )
        open_trades.remove(trade)
        recorded += 1

    if recorded:
        _save_open_gann_trades(open_trades)


# ---------------------------------------------------------------------------
# Equity snapshot
# ---------------------------------------------------------------------------
def _append_equity_snapshot():
    """Store current variant equity = effective capital + realized PnL + unrealized PnL."""
    base = _effective_capital()
    realized = sum(t["pnl"] for t in store.load_trades_variant(VARIANT))
    unrealized = 0.0
    pos = _get_position()
    if pos:
        unrealized = float(pos.get("unrealisedPnl", 0) or 0)
    equity = base + realized + unrealized
    ts = time.time()
    store.append_variant_equity_point(VARIANT, ts, equity)
    return equity


# ---------------------------------------------------------------------------
# Entry / exit / trailing stop
# ---------------------------------------------------------------------------
def _enter_position(direction: int, close: float, basis: float, instr: dict):
    equity = _effective_capital()
    qty = _compute_entry_size(close, equity, instr)
    if qty is None:
        log.warning("Insufficient capital to enter Gann forward position at close=%.2f (equity=%.2f)", close, equity)
        return None

    side = _int_to_side(direction)
    stop = stop_price_for_direction(direction, basis, STOP_LOSS_PCT)
    if stop is None or stop <= 0:
        log.warning("Could not compute valid stop price for Gann entry")
        return None
    stop = _round_price(stop, instr["tick_size"])

    try:
        result = client.place_market_order(SYMBOL, side, qty, stop_loss=stop)
        order_id = result.get("orderId", "")
        log.info(
            "Gann forward ENTER | side=%s qty=%.3f close=%.2f stop=%.2f orderId=%s",
            side, qty, close, stop, order_id,
        )
        open_trades = _load_open_gann_trades()
        open_trades.append(
            {
                "order_id": order_id,
                "side": side,
                "qty": qty,
                "entry_price": close,
                "entry_ts_ms": int(time.time() * 1000),
                "stop_price": stop,
                "stop_basis": basis,
                "current_sl": stop,
            }
        )
        _save_open_gann_trades(open_trades)
        return {"direction": direction, "qty": qty, "avg_price": close, "stop_basis": basis, "stop_price": stop}
    except Exception as exc:
        log.exception("Gann forward entry failed: %s", exc)
        return None


def _close_position(position: dict, reason: str):
    side = position.get("side", "")
    close_side = "Sell" if side == "Buy" else "Buy"
    qty = abs(float(position.get("size", 0) or 0))
    try:
        result = client.place_market_order(SYMBOL, close_side, qty)
        log.info(
            "Gann forward CLOSE | reason=%s side=%s qty=%.3f orderId=%s",
            reason, close_side, qty, result.get("orderId"),
        )
        # Give Bybit a moment to book the closed-PnL row
        time.sleep(2)
        _record_closed_trade()
    except Exception as exc:
        log.exception("Gann forward close failed: %s", exc)


def _update_trailing_stop(position: dict, df: pd.DataFrame, completed_idx: int, instr: dict):
    """Move the server-side stop-loss to the latest higher bottom / lower top."""
    side = position.get("side", "")
    direction = _position_side_to_int(side)

    if direction == DIRECTION_LONG:
        new_basis = latest_higher_bottom(completed_idx - 1, df["overnight_trend"], df["low"])
        new_stop = new_basis * (1 - STOP_LOSS_PCT / 100)
    else:
        new_basis = latest_lower_top(completed_idx - 1, df["overnight_trend"], df["high"])
        new_stop = new_basis * (1 + STOP_LOSS_PCT / 100)

    current_stop = float(position.get("stopLoss", 0) or 0)
    if new_stop <= 0:
        return

    new_stop = _round_price(new_stop, instr["tick_size"])

    # Only move stop in favourable direction
    if direction == DIRECTION_LONG and new_stop > current_stop:
        should_update = True
    elif direction == DIRECTION_SHORT and (current_stop == 0 or new_stop < current_stop):
        should_update = True
    else:
        should_update = False

    if should_update:
        try:
            client.set_trading_stop(SYMBOL, stop_loss=new_stop)
            log.info("Gann forward trailing stop moved to %.2f", new_stop)
            open_trades = _load_open_gann_trades()
            pos_qty = abs(float(position.get("size", 0) or 0))
            pos_entry = float(position.get("avgPrice", 0) or 0)
            for trade in open_trades:
                if (
                    trade.get("side") == side
                    and abs(float(trade.get("qty", 0) or 0) - pos_qty) <= 0.01
                    and abs(float(trade.get("entry_price", 0) or 0) - pos_entry) <= max(pos_entry, 1) * 0.02
                ):
                    trade["current_sl"] = new_stop
                    break
            _save_open_gann_trades(open_trades)
        except Exception as exc:
            log.warning("Could not update Gann trailing stop: %s", exc)


# ---------------------------------------------------------------------------
# Core bot logic
# ---------------------------------------------------------------------------
def _process_new_day(latest_day: pd.Timestamp, instr: dict):
    """Process the daily candle that just closed (index -2) at 00:00 UTC."""
    # Catch any closed-PnL rows that appeared since the last run before deciding
    # whether a new position is needed.
    _record_closed_trade()

    df = prepare_dataframe(_fetch_daily_klines())
    if len(df) < 3:
        log.warning("Not enough daily history to compute Gann signal")
        return

    # The newest row is the candle that just opened at 00:00 UTC; use the
    # completed candle immediately before it for the signal and entry price.
    completed_idx = len(df) - 2
    completed_row = df.iloc[completed_idx]
    desired_dir, basis = signal_for_day(df, completed_idx, direction=DIRECTION)
    weekly_trend = int(completed_row["weekly_trend"])
    close = float(completed_row["close"])

    position = _get_position()

    if position is not None:
        pos_side = _position_side_to_int(position.get("side", ""))
        pos_stop = float(position.get("stopLoss", 0) or 0)
        high = float(completed_row["high"])
        low = float(completed_row["low"])

        # Stop-loss hit intra-day on the completed candle?
        stop_hit = (pos_side == DIRECTION_LONG and low <= pos_stop) or (
            pos_side == DIRECTION_SHORT and high >= pos_stop
        )
        weekly_flipped = pos_side != weekly_trend
        trend_reversed = desired_dir is not None and pos_side != desired_dir

        if stop_hit or weekly_flipped or trend_reversed:
            reason = "stop_loss" if stop_hit else ("weekly_flip" if weekly_flipped else "trend_reversal")
            _close_position(position, reason)
            position = None

    if position is None and desired_dir is not None and desired_dir == weekly_trend:
        _enter_position(desired_dir, close, basis, instr)
    elif position is not None:
        _update_trailing_stop(position, df, completed_idx, instr)

    equity = _append_equity_snapshot()
    _state["last_processed_day"] = latest_day.strftime("%Y-%m-%d")
    log.info("Gann forward day processed | signal=%s equity=%.2f", desired_dir, equity)


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(_state).encode())

    def log_message(self, format, *args):
        pass


def _start_health_server():
    def serve():
        with socketserver.TCPServer(("", HEALTH_PORT), _HealthHandler) as httpd:
            httpd.serve_forever()

    threading.Thread(target=serve, daemon=True).start()
    log.info("Health endpoint on http://127.0.0.1:%s/health", HEALTH_PORT)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    store.init()
    instr = _get_instrument_info()
    _state["instr"] = instr
    log.info("Instrument info | qty_step=%s min_qty=%s tick_size=%s", instr["qty_step"], instr["min_qty"], instr["tick_size"])

    try:
        client.set_leverage(SYMBOL, LEVERAGE)
        log.info("Leverage set to %.1fx for %s", LEVERAGE, SYMBOL)
    except Exception as exc:
        log.warning("Could not set leverage: %s", exc)

    _start_health_server()
    _state["running"] = True

    # Use the actual Bybit balance as the starting capital if it is lower.
    effective_capital = _effective_capital()
    store.rebuild_variant_equity_curve(
        VARIANT, effective_capital, store.load_trades_variant(VARIANT)
    )

    log.info(
        "Gann forward bot started | effective_capital=%.2f leverage=%.1fx base=%.0f%%",
        effective_capital, LEVERAGE, BASE_UNIT_NOTIONAL_PCT * 100,
    )

    # Reconcile local Gann trade tracker with Bybit position on startup.
    try:
        pos = _get_position()
        open_trades = _load_open_gann_trades()
        if pos and abs(float(pos.get("size", 0) or 0)) > 0:
            if not open_trades:
                log.warning("Bybit shows an open Gann position but local tracker is empty; reconstructing tracker.")
                open_trades.append(
                    {
                        "order_id": "unknown",
                        "side": pos.get("side", ""),
                        "qty": abs(float(pos.get("size", 0) or 0)),
                        "entry_price": float(pos.get("avgPrice", 0) or 0),
                        "entry_ts_ms": int(pos.get("createdTime", 0) or 0),
                        "stop_price": float(pos.get("stopLoss", 0) or 0),
                        "stop_basis": 0,
                        "current_sl": float(pos.get("stopLoss", 0) or 0),
                    }
                )
                _save_open_gann_trades(open_trades)
        else:
            if open_trades:
                log.warning("No open Bybit position but %s Gann trade(s) tracked; clearing stale tracker.", len(open_trades))
                _save_open_gann_trades([])
    except Exception as exc:
        log.warning("Could not reconcile Gann trade tracker at startup: %s", exc)

    while True:
        try:
            now = datetime.now(timezone.utc)
            # Daily candle closes at 00:00 UTC; process shortly after.
            if now.hour == 0 and now.minute < 10:
                latest_day = pd.Timestamp(now.date(), tz="UTC")
                last = _state.get("last_processed_day")
                if last != latest_day.strftime("%Y-%m-%d"):
                    _process_new_day(latest_day, instr)
                    time.sleep(600)  # avoid reprocessing the same day
            # Record an equity snapshot every hour for the dashboard
            _append_equity_snapshot()
            time.sleep(300)
        except Exception as exc:
            _state["error_count"] += 1
            log.exception("Gann forward bot main-loop error: %s", exc)
            time.sleep(60)


if __name__ == "__main__":
    main()
