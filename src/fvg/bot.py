"""
Live + paper forward-test bot for the SSRN-6725880 A/A+ Momentum FVG strategy.

- Reads public M15/H1 klines from Bybit linear perpetual BTCUSDT.
- Generates setups with the SSRN FVG strategy engine.
- Splits live Bybit demo equity into two variants (base / tight stop).
- The "base" variant is executed LIVE on Bybit demo (market entry + server-side
  stop-loss at strategy stop + server-side take-profit at runner max).
- The "tight" variant remains virtual/paper for comparison.
- Stores closed paper/live trades + per-variant equity snapshots in bot.db.
- Exposes a /health endpoint on port 5557.
"""

from __future__ import annotations

import http.server
import json
import logging
import math
import socketserver
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared import notify
from src.shared import store
from src.shared.bybit_client import BybitClient
from src.fvg.strategy import Direction, generate_setups, diagnose_latest_bar

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SYMBOL = "BTCUSDT"
M15_INTERVAL = "15"
M15_BARS = 501  # fetch 501 so we can drop the forming bar and still keep 500 closed bars
H1_INTERVAL = "60"
H1_BARS = 1000  # fetch native H1 directly (do not resample M15) to match backtest HTF levels

# Fixed trading capital for the single tight variant. The other half of the
# Bybit account is left untraded as a buffer.
TRADING_CAPITAL = 92_453.04
RISK_PER_TRADE = TRADING_CAPITAL * 0.005      # fixed $462.27 per trade
DAILY_LOSS_CAP = RISK_PER_TRADE * 2.0          # $924.53 daily loss cap
LEVERAGE = 2                                   # required so position size can exceed allocated cash
ENTRY_FEE_PCT = 0.0001                        # maker entry
EXIT_FEE_PCT = 0.0006                         # taker exit

VARIANTS = {
    "tight": {
        "stop_pct": 0.004,
        "tp1_pct": 0.004,
        "runner_a_pct": 0.020,
        "runner_a_plus_pct": 0.040,
        "htf_proximity_pct": 0.003,
    },
}

COMMON_PARAMS = {
    "fvg_lookforward": 8,
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "direction_filter": None,
    # TP2 is tracked but not used as an exit; keep paper values aligned with backtest
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}

HEALTH_PORT = 5557

# Place real orders on the Bybit demo account for the tight variant.
LIVE_VARIANTS = {"tight"}

# Shared state for the /health endpoint
_state = {
    "running": False,
    "allocated_capital": 0.0,
    "capital": {},
    "open_trades": {},
    "open_live_trades": [],
    "last_processed_ts": None,
    "error_count": 0,
}

BASE_DIR = config.PROJECT_ROOT
LOG_PATH = BASE_DIR / "logs" / "fvg_paper_bot.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("fvg_paper_bot")

# ---------------------------------------------------------------------------
# Bybit helpers
# ---------------------------------------------------------------------------
client = BybitClient(
    config.BYBIT_API_KEY,
    config.BYBIT_API_SECRET,
    config.BYBIT_DEMO_BASE_URL,
    config.BYBIT_PUBLIC_BASE_URL,
)


def _get_instrument_lot_info():
    """Fetch qtyStep and minOrderQty for SYMBOL."""
    try:
        info = client.get_instrument(SYMBOL)
        lsf = info.get("lotSizeFilter", {})
        qty_step = float(lsf.get("qtyStep", "0.001"))
        min_qty = float(lsf.get("minOrderQty", "0.001"))
        log.info("Instrument lot info: qtyStep=%s minOrderQty=%s", qty_step, min_qty)
        return qty_step, min_qty
    except Exception as exc:
        log.warning("Could not fetch instrument info (%s); using defaults", exc)
        return 0.001, 0.001


def _get_allocated_capital():
    """Return the fixed trading capital for the single tight variant."""
    try:
        balance = client.get_wallet_balance()
        total_equity = float(balance.get("totalEquity", 0))
        if total_equity <= 0:
            raise ValueError(f"Bybit returned non-positive equity: {total_equity}")
        log.info(
            "Bybit total equity $%.2f -> trading capital $%.2f (single tight variant)",
            total_equity, TRADING_CAPITAL,
        )
    except Exception as exc:
        log.warning("Could not read Bybit equity (%s); using fixed trading capital", exc)
    return TRADING_CAPITAL


def _restore_capital_for_variant(variant: str, allocated_capital: float) -> float:
    """Rebuild the variant equity curve from closed trades and return current capital.

    On every restart the in-memory `capital` dict must continue from where the
    previous run left off.  We reconstruct the realized equity series from the
    trades table so that restarts do not erase previously realized PnL.
    """
    trades = store.load_trades_variant(variant)
    if not trades:
        return allocated_capital

    equity = store.rebuild_variant_equity_curve(variant, allocated_capital, trades)
    log.info(
        "Restored %s capital from %s closed trade(s): $%.2f",
        variant, len(trades), equity,
    )
    return equity


def _set_symbol_leverage():
    """Set account leverage for SYMBOL (best-effort; ignore if not allowed)."""
    try:
        client.set_leverage(SYMBOL, LEVERAGE)
        log.info("Set %s leverage to %sx", SYMBOL, LEVERAGE)
    except Exception as exc:
        log.warning("Could not set leverage (%s); using account default", exc)


def _floor_qty(qty: float, step: float) -> float:
    return math.floor(qty / step) * step


def _klines_to_df(rows: list) -> pd.DataFrame:
    """Convert Bybit kline rows (newest first) to an ascending DataFrame."""
    rows = sorted(rows, key=lambda r: int(r[0]))
    df = pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["open_time"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    return df[["timestamp", "open", "high", "low", "close", "volume", "turnover"]]


def _fetch_history(interval: str, limit: int) -> pd.DataFrame:
    rows = client.get_klines(SYMBOL, interval, limit=limit)
    return _klines_to_df(rows)


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------
@dataclass
class PaperTrade:
    variant: str
    setup: object  # strategy.Setup
    size_usd: float
    qty: float
    entry_cost: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    trailing_stop: float | None = None
    extreme_price: float = 0.0

    def __post_init__(self):
        if self.extreme_price == 0.0:
            self.extreme_price = self.setup.entry_price


def _setup_signature(setup) -> tuple:
    return (setup.timestamp, setup.direction.name, setup.grade.value, setup.entry_price)


def _compute_size(entry_price: float, stop_price: float, capital: float, qty_step: float, min_qty: float):
    # Fixed-dollar risk per trade, capped to available margin.
    stop_distance = abs(entry_price - stop_price) / entry_price
    if stop_distance <= 0:
        return None, None
    size_usd = RISK_PER_TRADE / stop_distance
    max_size_usd = capital * LEVERAGE
    if size_usd > max_size_usd:
        log.warning(
            "Position size capped from $%.2f to $%.2f (capital $%.2f × leverage %s)",
            size_usd, max_size_usd, capital, LEVERAGE,
        )
        size_usd = max_size_usd
    qty = _floor_qty(size_usd / entry_price, qty_step)
    if qty < min_qty:
        return None, None
    return size_usd, qty


def _create_trade(variant: str, setup, capital: float, qty_step: float, min_qty: float) -> PaperTrade | None:
    size_usd, qty = _compute_size(setup.entry_price, setup.stop_price, capital, qty_step, min_qty)
    if size_usd is None:
        return None
    entry_cost = size_usd * ENTRY_FEE_PCT
    return PaperTrade(
        variant=variant,
        setup=setup,
        size_usd=size_usd,
        qty=qty,
        entry_cost=entry_cost,
    )


def _place_live_trade(variant: str, setup, capital: float, qty_step: float, min_qty: float) -> dict | None:
    """Place a real Bybit demo market order with hard SL + hard runner TP.

    The trailing stop is managed manually in-code after TP1 is hit, so no
    server-side trailing stop is attached at entry.
    Returns a dict with trade details if successful, else None.
    """
    size_usd, qty = _compute_size(setup.entry_price, setup.stop_price, capital, qty_step, min_qty)
    if size_usd is None:
        log.warning("%s live order skipped: qty below minimum", variant)
        return None

    side = "Buy" if setup.direction == Direction.LONG else "Sell"
    trail_dist = abs(setup.entry_price - setup.tp1_price)
    try:
        result = client.place_market_order(
            SYMBOL,
            side,
            qty,
            stop_loss=setup.stop_price,
            take_profit=setup.runner_max_price,
        )
        order_id = result.get("orderId")

        log.info(
            "LIVE %s order placed | side=%s qty=%.3f entry=%.2f stop=%.2f runner=%.2f tp1=%.2f trail=%.2f orderId=%s",
            variant, side, qty, setup.entry_price, setup.stop_price,
            setup.runner_max_price, setup.tp1_price, trail_dist, order_id,
        )
        notify.notify_entry(
            variant, side, qty, setup.entry_price, setup.stop_price,
            setup.tp1_price, setup.runner_max_price, live=True,
        )
        return {
            "order_id": order_id,
            "variant": variant,
            "side": side,
            "qty": qty,
            "entry_price": setup.entry_price,
            "entry_ts_ms": int(setup.timestamp.timestamp() * 1000),
            "stop_price": setup.stop_price,
            "tp1_price": setup.tp1_price,
            "runner_max_price": setup.runner_max_price,
            "trail_dist": trail_dist,
            "tp1_hit": False,
            "extreme_price": setup.entry_price,
            "current_sl": setup.stop_price,
        }
    except Exception as exc:
        log.exception("LIVE %s order failed: %s", variant, exc)
        return None


def _load_open_live_trades() -> list[dict]:
    """Load open live trades from earlier runs, back-filling legacy fields."""
    raw = store.get_meta("open_live_trades", "")
    if not raw:
        return []
    try:
        trades = json.loads(raw)
    except Exception:
        return []

    for t in trades:
        # Backwards compatibility for dicts written before manual trailing SL
        if "runner_max_price" not in t:
            t["runner_max_price"] = t.get("take_profit", t.get("entry_price", 0.0))
        if "tp1_hit" not in t:
            t["tp1_hit"] = False
        if "extreme_price" not in t or float(t.get("extreme_price", 0.0)) == 0.0:
            t["extreme_price"] = float(t.get("entry_price", 0.0))
        if "current_sl" not in t:
            t["current_sl"] = float(t.get("stop_price", 0.0))
        if "trail_dist" not in t or float(t.get("trail_dist", 0.0)) == 0.0:
            entry = float(t.get("entry_price", 0.0))
            tp1 = float(t.get("tp1_price", entry))
            t["trail_dist"] = abs(entry - tp1)
    return trades


def _save_open_live_trades(trades: list[dict]):
    store.set_meta("open_live_trades", json.dumps(trades, default=str))


def _update_live_trailing_sl(open_live_trades: list[dict], latest_bar: pd.Series):
    """Manually manage trailing SL for open live trades to match backtest logic.

    Backtest exit order: hard SL -> runner-max hard TP -> trailing SL after TP1.
    TP1 activation and the trailing SL are computed from M15 bar OHLC.
    Only calls Bybit when the SL actually moves.
    """
    if not open_live_trades:
        return

    o, h, l, c = latest_bar["open"], latest_bar["high"], latest_bar["low"], latest_bar["close"]
    bar_ts = latest_bar["timestamp"]

    for trade in open_live_trades:
        side = trade.get("side", "")
        entry = float(trade.get("entry_price", 0.0))
        stop = float(trade.get("stop_price", 0.0))
        tp1 = float(trade.get("tp1_price", 0.0))
        trail_dist = float(trade.get("trail_dist", abs(entry - tp1)))
        tp1_hit = bool(trade.get("tp1_hit", False))
        extreme = float(trade.get("extreme_price", entry))
        current_sl = float(trade.get("current_sl", stop))

        if entry == 0.0 or stop == 0.0 or tp1 == 0.0:
            continue

        is_long = side == "Buy"

        # Update extreme price
        if is_long:
            extreme = max(extreme, h)
        else:
            extreme = min(extreme, l)
        trade["extreme_price"] = extreme

        # Check TP1 activation on this bar
        sl_needs_update = False
        if not tp1_hit:
            hit = (is_long and h >= tp1) or (not is_long and l <= tp1)
            if hit:
                tp1_hit = True
                trade["tp1_hit"] = True
                # First trailing SL is max(tp1, extreme - trail_dist) for long
                if is_long:
                    current_sl = max(tp1, extreme - trail_dist)
                else:
                    current_sl = min(tp1, extreme + trail_dist)
                sl_needs_update = True
                log.info(
                    "LIVE %s TP1 hit at %.2f | new trailing SL=%.2f bar=%s",
                    trade.get("variant"), tp1, current_sl, bar_ts,
                )
                notify.notify_sl_trail(
                    trade.get("variant", ""), tp1, current_sl, extreme, bar_ts, live=True,
                )

        # Update trailing SL if already activated
        if tp1_hit:
            if is_long:
                new_sl = max(current_sl, extreme - trail_dist)
            else:
                new_sl = min(current_sl, extreme + trail_dist)

            if abs(new_sl - current_sl) > 1e-9:
                current_sl = new_sl
                sl_needs_update = True

            if sl_needs_update:
                try:
                    # If current price already violates the new trailing SL, close now
                    # instead of trying to set an invalid server-side stop.
                    last_price = client.get_last_price(SYMBOL)
                    if (is_long and last_price <= current_sl) or (
                        not is_long and last_price >= current_sl
                    ):
                        log.info(
                            "LIVE %s trailing SL already hit (last=%.2f, sl=%.2f); closing immediately",
                            trade.get("variant"), last_price, current_sl,
                        )
                        _close_live_position_immediately(trade, "trail_1r_immediate")
                    else:
                        old_sl = float(trade.get("current_sl", current_sl))
                        client.set_trading_stop(SYMBOL, stop_loss=current_sl)
                        log.info(
                            "LIVE %s trailing SL moved %.2f -> %.2f (extreme=%.2f) bar=%s",
                            trade.get("variant"),
                            old_sl,
                            current_sl,
                            extreme,
                            bar_ts,
                        )
                        notify.notify_sl_trail(
                            trade.get("variant", ""), old_sl, current_sl, extreme, bar_ts, live=True,
                        )
                except Exception as exc:
                    log.warning(
                        "LIVE %s failed to move trailing SL to %.2f: %s",
                        trade.get("variant"), current_sl, exc,
                    )
            trade["current_sl"] = current_sl


def _close_live_position_immediately(trade: dict, reason: str):
    """Close a live position at market when the trailing SL is already hit."""
    side = trade.get("side", "")
    qty = float(trade.get("qty", 0) or 0)
    if qty <= 0:
        return False
    close_side = "Sell" if side == "Buy" else "Buy"
    try:
        result = client.place_market_order(SYMBOL, close_side, qty)
        log.info(
            "LIVE %s position closed immediately | side=%s qty=%.3f reason=%s orderId=%s",
            trade.get("variant"), close_side, qty, reason, result.get("orderId"),
        )
        return True
    except Exception as exc:
        log.warning("LIVE %s immediate close failed: %s", trade.get("variant"), exc)
        return False


def _match_live_trade(closed_row: dict, open_trades: list[dict]) -> dict | None:
    """Find the open live trade that corresponds to a closed-PnL row.

    Bybit gives the SL/TP fill a different orderId than the original market
    order, so we match by quantity, entry price, and opposite closing side.
    """
    try:
        close_side = closed_row.get("side", "")
        qty = float(closed_row.get("qty", 0) or 0)
        entry_price = float(closed_row.get("avgEntryPrice", 0) or 0)
        updated_ms = int(closed_row.get("updatedTime", 0) or 0)
    except Exception:
        return None

    for trade in open_trades:
        # Closing side must be opposite of opening side
        if trade.get("side") == "Buy" and close_side != "Sell":
            continue
        if trade.get("side") == "Sell" and close_side != "Buy":
            continue
        qty_diff = abs(float(trade.get("qty", 0) or 0) - qty)
        if qty_diff > 0.01:
            continue
        entry = float(trade.get("entry_price", 0) or 0)
        entry_tol = max(entry, entry_price) * 0.02  # allow 2% fill slippage
        if entry > 0 and entry_price > 0 and abs(entry - entry_price) > entry_tol:
            continue
        if updated_ms < int(trade.get("entry_ts_ms", 0) or 0):
            continue
        return trade
    return None


def _infer_live_exit_reason(trade: dict, exit_price: float) -> str:
    """Infer why a live position closed based on stored strategy levels."""
    stop = float(trade.get("stop_price", 0.0) or 0.0)
    runner = float(trade.get("runner_max_price", 0.0) or 0.0)
    current_sl = float(trade.get("current_sl", stop) or stop)
    tol = abs(float(trade.get("entry_price", exit_price))) * 0.001

    if stop > 0 and abs(exit_price - stop) <= tol:
        return "stop_loss"
    if runner > 0 and abs(exit_price - runner) <= tol:
        return "runner_max"
    if current_sl != stop and abs(exit_price - current_sl) <= tol:
        return "trail_1r"
    return "manual/other"


def _sync_live_closed_pnl(open_live_trades: list[dict], capital: dict, daily_pnl: dict):
    """Poll Bybit closed-PnL and record any trades from our live positions."""
    if not open_live_trades:
        return

    try:
        closed = client.get_all_closed_pnl(SYMBOL)
    except Exception as exc:
        log.warning("Could not fetch closed PnL: %s", exc)
        return

    recorded = 0
    for row in closed:
        trade = _match_live_trade(row, open_live_trades)
        if trade is None:
            continue

        variant = trade["variant"]
        try:
            store.upsert_trade(row, variant=variant)
        except Exception as exc:
            log.warning("Failed to record live closed-PnL for %s: %s", trade.get("order_id"), exc)
            continue

        pnl = float(row.get("closedPnl", 0) or 0)
        exit_price = float(row.get("avgExitPrice", 0) or 0)
        exit_reason = _infer_live_exit_reason(trade, exit_price)
        capital[variant] += pnl
        close_ts_ms = int(row.get("updatedTime", 0) or 0)
        if close_ts_ms:
            close_day = pd.to_datetime(close_ts_ms, unit="ms", utc=True).normalize()
            daily_pnl[variant][close_day] = daily_pnl[variant].get(close_day, 0.0) + pnl
            store.append_variant_equity_point(variant, close_ts_ms / 1000, capital[variant])
        entry_price = float(row.get("avgEntryPrice", 0))
        qty = float(row.get("qty", 0) or 0)
        log.info(
            "LIVE %s trade CLOSED | reason=%s entry=%.2f exit=%.2f qty=%s pnl=%.2f equity=%.2f",
            variant,
            exit_reason,
            entry_price,
            exit_price,
            row.get("qty"),
            pnl,
            capital[variant],
        )
        notify.notify_exit(
            variant, trade.get("side", ""), qty, entry_price, exit_price,
            pnl, exit_reason, capital[variant], live=True,
        )
        open_live_trades.remove(trade)
        recorded += 1

    if recorded:
        _save_open_live_trades(open_live_trades)


def _process_bar(trade: PaperTrade, bar: pd.Series) -> dict | None:
    """Advance one trade by one closed bar. Returns a closed-trade dict or None."""
    direction = trade.setup.direction
    entry = trade.setup.entry_price
    stop = trade.setup.stop_price
    tp1 = trade.setup.tp1_price
    tp2 = trade.setup.tp2_price
    runner_max = trade.setup.runner_max_price
    trail_dist = abs(entry - tp1)

    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

    # Update extreme for trailing stop
    if direction == Direction.LONG:
        trade.extreme_price = max(trade.extreme_price, h)
    else:
        trade.extreme_price = min(trade.extreme_price, l)

    exit_price = None
    exit_reason = None

    # Stop loss
    if direction == Direction.LONG and l <= stop:
        exit_price = min(o, stop) if c >= stop else stop
        exit_reason = "stop_loss"
    elif direction == Direction.SHORT and h >= stop:
        exit_price = max(o, stop) if c <= stop else stop
        exit_reason = "stop_loss"
    else:
        # TP1 activation
        if not trade.tp1_hit:
            hit_tp1 = (direction == Direction.LONG and h >= tp1) or (
                direction == Direction.SHORT and l <= tp1
            )
            if hit_tp1:
                trade.tp1_hit = True
                if direction == Direction.LONG:
                    trade.trailing_stop = max(tp1, trade.extreme_price - trail_dist)
                else:
                    trade.trailing_stop = min(tp1, trade.extreme_price + trail_dist)

        # Update trailing stop
        if trade.tp1_hit:
            if direction == Direction.LONG:
                trade.trailing_stop = max(trade.trailing_stop, trade.extreme_price - trail_dist)
            else:
                trade.trailing_stop = min(trade.trailing_stop, trade.extreme_price + trail_dist)

        # TP2 flag (tracked but not an exit)
        if not trade.tp2_hit:
            hit_tp2 = (direction == Direction.LONG and h >= tp2) or (
                direction == Direction.SHORT and l <= tp2
            )
            if hit_tp2:
                trade.tp2_hit = True

        # Runner max cap (hard take-profit for the runner portion)
        hit_runner = (direction == Direction.LONG and h >= runner_max) or (
            direction == Direction.SHORT and l <= runner_max
        )
        if hit_runner:
            exit_price = runner_max
            exit_reason = "runner_max"
        elif trade.tp1_hit:
            if direction == Direction.LONG and l <= trade.trailing_stop:
                exit_price = min(o, trade.trailing_stop) if c >= trade.trailing_stop else trade.trailing_stop
                exit_reason = "trail_1r"
            elif direction == Direction.SHORT and h >= trade.trailing_stop:
                exit_price = max(o, trade.trailing_stop) if c <= trade.trailing_stop else trade.trailing_stop
                exit_reason = "trail_1r"

    if exit_reason is None:
        return None

    # P&L
    if direction == Direction.LONG:
        gross_pnl = trade.size_usd * (exit_price - entry) / entry
    else:
        gross_pnl = trade.size_usd * (entry - exit_price) / entry

    exit_cost = trade.size_usd * EXIT_FEE_PCT
    total_cost = trade.entry_cost + exit_cost
    net_pnl = gross_pnl - total_cost

    risk_amount = trade.size_usd * abs(entry - stop) / entry
    result_r = net_pnl / risk_amount if risk_amount > 0 else 0.0

    exit_ts_ms = int(bar["timestamp"].timestamp() * 1000)

    side = "Buy" if direction == Direction.LONG else "Sell"

    raw = {
        "grade": trade.setup.grade.value,
        "htf_aligned": trade.setup.htf_aligned,
        "momentum_leg_idx": trade.setup.momentum_leg_idx,
        "gross_pnl": gross_pnl,
        "entry_cost": trade.entry_cost,
        "exit_cost": exit_cost,
        "total_cost": total_cost,
        "result_r": result_r,
        "exit_reason": exit_reason,
        "tp1_hit": trade.tp1_hit,
        "tp2_hit": trade.tp2_hit,
        "trailing_stop": trade.trailing_stop,
        "extreme_price": trade.extreme_price,
        "size_usd": trade.size_usd,
    }

    return {
        "id": f"{trade.variant}-{uuid.uuid4().hex[:12]}",
        "variant": trade.variant,
        "symbol": SYMBOL,
        "side": side,
        "qty": trade.qty,
        "entry_price": entry,
        "exit_price": exit_price,
        "pnl": net_pnl,
        "exit_ts": exit_ts_ms,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _seconds_to_next_bar(interval_min: int = 15, offset_sec: int = 5) -> float:
    now = datetime.now(timezone.utc).timestamp()
    period = interval_min * 60
    next_bar = ((int(now) // period) + 1) * period + offset_sec
    return max(0.0, next_bar - now)


# ---------------------------------------------------------------------------
# Health-check HTTP server
# ---------------------------------------------------------------------------
class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            payload = {
                "status": "ok" if _state["running"] else "starting",
                "symbol": SYMBOL,
                "allocated_capital": _state["allocated_capital"],
                "variant_capital": {k: round(v, 2) for k, v in _state["capital"].items()},
                "open_trades": {k: len(v) for k, v in _state["open_trades"].items()},
                "open_live_trades": _state.get("open_live_trades", []),
                "live_trade_count": len(_state.get("open_live_trades", [])),
                "last_processed_ts": str(_state["last_processed_ts"]),
                "error_count": _state["error_count"],
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default access logs; the bot log is noisy enough
        pass


def _start_health_server():
    def _serve():
        # Allow immediate port reuse after a restart so the health check stays
        # available even when the bot is quickly restarted.
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", HEALTH_PORT), _HealthHandler) as httpd:
            httpd.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    log.info("Health endpoint running on http://127.0.0.1:%s/health", HEALTH_PORT)


def _heartbeat_loop(interval_seconds: int) -> None:
    """Send a periodic status notification so the operator knows the bot is alive."""
    # Wait one full interval before the first heartbeat so we do not spam on
    # a quick restart.
    time.sleep(interval_seconds)
    while True:
        try:
            open_paper = sum(len(v) for v in _state.get("open_trades", {}).values())
            open_live = len(_state.get("open_live_trades", []))
            last_ts = _state.get("last_processed_ts", "None")
            equity = _state.get("capital", {})
            equity_str = ", ".join(
                f"{k}: ${v:,.2f}" for k, v in equity.items()
            )
            notify.send(
                f"<b>FVG Bot Heartbeat</b>\n"
                f"Status: OK\n"
                f"Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Last processed bar: {last_ts}\n"
                f"Open paper trades: {open_paper}\n"
                f"Open live trades: {open_live}\n"
                f"Equity: {equity_str or 'n/a'}",
                subject="FVG Bot Heartbeat OK",
            )
            log.info("Heartbeat notification sent")
        except Exception as exc:
            log.warning("Heartbeat notification failed: %s", exc)
        time.sleep(interval_seconds)


def _start_heartbeat() -> None:
    interval = getattr(config, "HEARTBEAT_INTERVAL_SECONDS", 900)
    if interval <= 0:
        log.info("Heartbeat notifications disabled (interval=%s)", interval)
        return
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(interval,),
        daemon=True,
        name="fvg-heartbeat",
    )
    thread.start()
    log.info("Heartbeat notifications every %s seconds", interval)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    log.info("Starting FVG live/paper forward-test bot")
    log.info("Variants: %s", list(VARIANTS.keys()))
    log.info("Live variants: %s", sorted(LIVE_VARIANTS))

    store.init()
    # Clean up legacy tracking key no longer used
    try:
        store.set_meta("live_order_ids", "")
    except Exception:
        pass
    qty_step, min_qty = _get_instrument_lot_info()
    _set_symbol_leverage()

    # Capital is fixed at the trading capital for the single tight variant.
    allocated_capital = _get_allocated_capital()

    # Seed histories: fetch M15 and native H1 separately so HTF levels match
    # the backtest (which loads H1 directly from Bybit) rather than being
    # approximated by resampling M15.
    m15 = _fetch_history(M15_INTERVAL, M15_BARS)
    h1 = _fetch_history(H1_INTERVAL, H1_BARS)
    # Drop the currently forming bar on both timeframes; evaluate only closed bars.
    if len(m15) > 1:
        m15 = m15.iloc[:-1].copy().reset_index(drop=True)
    if len(h1) > 1:
        h1 = h1.iloc[:-1].copy().reset_index(drop=True)
    log.info("Seeded M15 history: %s bars, H1 history: %s bars", len(m15), len(h1))

    # Per-variant capital and open trades
    # Restore capital from closed trades so restarts do not reset realized PnL.
    capital = {variant: _restore_capital_for_variant(variant, allocated_capital) for variant in VARIANTS}
    initial_capital = {variant: allocated_capital for variant in VARIANTS}
    open_trades: dict[str, list[PaperTrade]] = {variant: [] for variant in VARIANTS}
    daily_pnl: dict[str, dict[pd.Timestamp, float]] = {variant: {} for variant in VARIANTS}

    # Track real Bybit demo positions placed by live variants
    open_live_trades: list[dict] = _load_open_live_trades()
    if open_live_trades:
        log.info("Loaded %s open live trade(s) from previous run", len(open_live_trades))
        for t in open_live_trades:
            log.info(
                "  OPEN %s %s | qty=%.3f entry=%.2f hard_sl=%.2f runner_tp=%.2f tp1=%.2f tp1_hit=%s current_sl=%.2f",
                t.get("variant"),
                t.get("side"),
                float(t.get("qty", 0) or 0),
                float(t.get("entry_price", 0) or 0),
                float(t.get("stop_price", 0) or 0),
                float(t.get("runner_max_price", 0) or 0),
                float(t.get("tp1_price", 0) or 0),
                bool(t.get("tp1_hit", False)),
                float(t.get("current_sl", 0) or 0),
            )
        # Try to record any trades that closed while the bot was down BEFORE
        # clearing stale trackers, otherwise missed closed-PnL is lost forever.
        _sync_live_closed_pnl(open_live_trades, capital, daily_pnl)
        _save_open_live_trades(open_live_trades)
        # Only clear unmatched trackers if Bybit confirms the position is flat.
        if open_live_trades:
            try:
                pos = client.get_position(SYMBOL)
                if pos and abs(float(pos.get("size", 0) or 0)) == 0:
                    log.warning(
                        "Bybit reports no open position but %s live trade(s) were still tracked after sync; clearing stale tracker.",
                        len(open_live_trades),
                    )
                    open_live_trades = []
                    _save_open_live_trades(open_live_trades)
            except Exception as exc:
                log.warning("Could not verify live position at startup: %s", exc)
    else:
        log.info("No open live trades loaded")

    last_processed_ts: pd.Timestamp | None = None

    # Publish state for /health endpoint
    _state["allocated_capital"] = allocated_capital
    _state["capital"] = capital
    _state["open_trades"] = open_trades
    _state["open_live_trades"] = open_live_trades
    _start_health_server()

    # Seed initial equity snapshots only if the curve is empty
    now_ts = time.time()
    for variant in VARIANTS:
        if not store.load_equity_curve_variant(variant):
            store.append_variant_equity_point(variant, now_ts, capital[variant])

    _state["running"] = True
    _start_heartbeat()

    # Sleep until just after the next 15-minute bar boundary (+ offset).
    # At that moment the previous M15 bar has just closed and is safe to process.
    sleep_s = _seconds_to_next_bar(15, 5)
    log.info("Sleeping %.1f seconds until next M15 bar boundary", sleep_s)
    time.sleep(sleep_s)

    while True:
        try:
            # Refresh histories: fetch M15 and native H1 directly from Bybit.
            # Native H1 ensures EMA-20/EMA-50/POC-30 levels match the backtest
            # instead of being approximated by resampling M15.
            m15 = _fetch_history(M15_INTERVAL, M15_BARS)
            h1 = _fetch_history(H1_INTERVAL, H1_BARS)
            # Bybit returns the current forming bar as the last row.  We must
            # evaluate setups only on the bar that has just closed; otherwise a
            # setup can look valid on a few seconds of price action and then
            # disappear once the full 15-minute bar completes.  This was the
            # root cause of forward-test trades that the classifier could not
            # reproduce later from completed bars.
            if len(m15) > 1:
                m15 = m15.iloc[:-1].copy().reset_index(drop=True)
            if len(h1) > 1:
                h1 = h1.iloc[:-1].copy().reset_index(drop=True)

            latest_ts = m15["timestamp"].iloc[-1]

            if last_processed_ts is None or latest_ts > last_processed_ts:
                latest_bar = m15.iloc[-1]

                # 1) Update open trades with the new bar
                for variant in VARIANTS:
                    still_open: list[PaperTrade] = []
                    for trade in open_trades[variant]:
                        closed = _process_bar(trade, latest_bar)
                        if closed is None:
                            still_open.append(trade)
                            continue

                        store.upsert_trade_variant(closed)
                        capital[variant] += closed["pnl"]
                        exit_day = latest_bar["timestamp"].normalize()
                        daily_pnl[variant][exit_day] = daily_pnl[variant].get(exit_day, 0.0) + closed["pnl"]
                        ts_sec = int(latest_bar["timestamp"].timestamp())
                        store.append_variant_equity_point(variant, ts_sec, capital[variant])
                        exit_reason = closed["raw"]["exit_reason"]
                        log.info(
                            "CLOSED %s %s trade | entry=%.2f exit=%.2f pnl=%.2f reason=%s equity=%.2f",
                            variant,
                            closed["side"],
                            closed["entry_price"],
                            closed["exit_price"],
                            closed["pnl"],
                            exit_reason,
                            capital[variant],
                        )
                        notify.notify_exit(
                            variant, closed["side"], closed["qty"],
                            closed["entry_price"], closed["exit_price"],
                            closed["pnl"], exit_reason, capital[variant], live=False,
                        )
                    open_trades[variant] = still_open
                    _state["capital"][variant] = capital[variant]
                    _state["open_trades"][variant] = still_open

                # 1a) Manually manage live trailing SL to match backtest logic
                _update_live_trailing_sl(open_live_trades, latest_bar)
                _save_open_live_trades(open_live_trades)

                # 1b) Record any real Bybit demo trades that closed since last bar
                _sync_live_closed_pnl(open_live_trades, capital, daily_pnl)
                _state["open_live_trades"] = open_live_trades

                # 2) Generate new setups that complete on this bar
                current_day = latest_ts.normalize()
                for variant, params in VARIANTS.items():
                    day_pnl = daily_pnl[variant].get(current_day, 0.0)
                    daily_cap = DAILY_LOSS_CAP
                    if day_pnl <= -daily_cap:
                        log.info(
                            "%s daily loss cap hit (day_pnl=%.2f, cap=%.2f); skipping new setups",
                            variant, day_pnl, daily_cap,
                        )
                        continue

                    try:
                        setups = generate_setups(
                            m15.copy(),
                            h1.copy(),
                            **COMMON_PARAMS,
                            **params,
                        )
                    except Exception as exc:
                        log.exception("generate_setups failed for %s: %s", variant, exc)
                        continue

                    seen = set()
                    new_setups = 0
                    for setup in setups:
                        if setup.timestamp != latest_ts:
                            continue
                        sig = _setup_signature(setup)
                        if sig in seen:
                            continue
                        seen.add(sig)

                        if variant in LIVE_VARIANTS:
                            # Only one live position per symbol in one-way mode
                            if any(t.get("variant") == variant for t in open_live_trades):
                                log.debug("%s already has an open live position; skipping", variant)
                                continue
                            try:
                                pos = client.get_position(SYMBOL)
                                if pos and abs(float(pos.get("size", 0) or 0)) > 0:
                                    log.debug("%s live position already open on Bybit; skipping", variant)
                                    continue
                            except Exception as exc:
                                log.warning("Could not check %s position: %s", variant, exc)
                                continue
                            live_trade = _place_live_trade(
                                variant, setup, capital[variant], qty_step, min_qty
                            )
                            if live_trade:
                                open_live_trades.append(live_trade)
                                _save_open_live_trades(open_live_trades)
                                new_setups += 1
                            continue

                        # Paper variant: simulate the trade
                        trade = _create_trade(variant, setup, capital[variant], qty_step, min_qty)
                        if trade is None:
                            log.debug("Skipping %s setup: qty below minimum", variant)
                            continue

                        open_trades[variant].append(trade)
                        new_setups += 1
                        side_str = "Buy" if setup.direction == Direction.LONG else "Sell"
                        log.info(
                            "NEW %s setup | %s entry=%.2f stop=%.2f tp1=%.2f runner=%.2f qty=%.3f",
                            variant,
                            setup.direction.name,
                            setup.entry_price,
                            setup.stop_price,
                            setup.tp1_price,
                            setup.runner_max_price,
                            trade.qty,
                        )
                        notify.notify_entry(
                            variant, side_str, trade.qty, setup.entry_price,
                            setup.stop_price, setup.tp1_price, setup.runner_max_price, live=False,
                        )

                    if new_setups:
                        log.info("%s: %s new setup(s) on %s", variant, new_setups, latest_ts)
                    else:
                        try:
                            diag = diagnose_latest_bar(
                                m15.copy(),
                                h1.copy(),
                                htf_proximity_pct=params.get("htf_proximity_pct", 0.002),
                                a_body_threshold=COMMON_PARAMS.get("a_body_threshold", 0.52),
                                a_plus_body_threshold=COMMON_PARAMS.get("a_plus_body_threshold", 0.70),
                                fvg_lookforward=COMMON_PARAMS.get("fvg_lookforward", 8),
                            )
                            reasons = "; ".join(diag["reasons"]) if diag["reasons"] else "all criteria met but no actionable setup"
                            log.info(
                                "%s: no setup on %s | session=%s | fvg=%s | leg=%s | "
                                "body_ratio=%s | htf=%s | reasons: %s",
                                variant,
                                latest_ts,
                                diag["session"],
                                diag["fvg_direction"] or "none",
                                f"idx{diag['leg_end_idx']} br={diag['leg_min_body_ratio']:.2f}" if diag["leg_end_idx"] is not None else "none",
                                f"{diag['leg_min_body_ratio']:.2f}" if diag["leg_min_body_ratio"] is not None else "n/a",
                                "aligned" if diag["htf_aligned"] else "not_aligned",
                                reasons,
                            )
                        except Exception as exc:
                            log.warning("diagnose_latest_bar failed for %s: %s", variant, exc)
                            log.info("%s: no setups on %s (strategy criteria not met)", variant, latest_ts)

                last_processed_ts = latest_ts
                _state["last_processed_ts"] = str(last_processed_ts)
            else:
                log.debug("No new closed bar yet (latest=%s)", latest_ts)

        except Exception as exc:
            _state["error_count"] += 1
            log.exception("Main loop error: %s", exc)

        # Sleep until just after the next 15-minute bar boundary.
        time.sleep(_seconds_to_next_bar(15, 5))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Fatal error in FVG paper bot; exiting")
        sys.exit(1)
