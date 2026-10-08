"""
Flask dashboard for the SSRN-6725880 FVG paper forward-test.

- Backtest tab: pre-computed tight-variant results.
- Paper tab: live tight-variant equity + trades from bot.db.

Runs on port 5556.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template

import config
import store
from bybit_client import BybitClient

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"

# Trading capital is fixed at half the initial Bybit account; the other half remains untraded.
TRADING_CAPITAL = 92_453.04

app = Flask(__name__)

client = BybitClient(
    config.BYBIT_API_KEY, config.BYBIT_API_SECRET,
    config.BYBIT_DEMO_BASE_URL, config.BYBIT_PUBLIC_BASE_URL,
)


def _get_account_split():
    """Return (total_equity, allocated_trading_capital)."""
    try:
        balance = client.get_wallet_balance()
        total = float(balance.get("totalEquity", 0))
        if total > 0:
            return total, TRADING_CAPITAL
    except Exception as exc:
        app.logger.warning("Could not read Bybit equity: %s", exc)
    # If Bybit cannot be reached, report the fixed trading capital anyway.
    return TRADING_CAPITAL * 2, TRADING_CAPITAL


def _load_backtest_json() -> dict | None:
    path = DATA_DIR / "fvg_backtest_tight.json"
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)


def _drawdown_from_equity(ts_list: list, values: list) -> dict:
    """Trailing drawdown: from the running equity peak."""
    peak = None
    dd_ts = []
    dd_vals = []
    for t, v in zip(ts_list, values):
        peak = v if peak is None else max(peak, v)
        dd_ts.append(t)
        dd_vals.append(round((v / peak - 1.0) * 100, 4) if peak else 0.0)
    return {"ts": dd_ts, "values": dd_vals}


def _static_drawdown_from_equity(ts_list: list, values: list, start_capital: float) -> dict:
    """Static drawdown: from the fixed starting capital."""
    dd_ts = []
    dd_vals = []
    for t, v in zip(ts_list, values):
        dd_ts.append(t)
        dd_vals.append(round((v / start_capital - 1.0) * 100, 4) if start_capital else 0.0)
    return {"ts": dd_ts, "values": dd_vals}


def _format_trade(t: dict) -> dict:
    """Normalize a trade dict for the dashboard trade log."""
    exit_ts = int(t.get("exit_ts", 0) or 0)
    return {
        "exit_time": datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        ),
        "variant": t.get("variant", ""),
        "side": t.get("side", ""),
        "qty": float(t.get("qty", 0) or 0),
        "entry": float(t.get("entry_price", 0) or 0),
        "exit": float(t.get("exit_price", 0) or 0),
        "pnl": float(t.get("pnl", 0) or 0),
    }


def _format_open_trade(t: dict, unrealized_pnl: float = 0.0) -> dict:
    """Normalize an open live trade for the dashboard trade log."""
    entry_ts = int(t.get("entry_ts_ms", 0) or 0)
    return {
        "exit_time": "OPEN",
        "variant": t.get("variant", ""),
        "side": t.get("side", ""),
        "qty": float(t.get("qty", 0) or 0),
        "entry": float(t.get("entry_price", 0) or 0),
        "exit": float(t.get("current_sl", 0) or 0),
        "pnl": unrealized_pnl,
        "is_open": True,
        "stop": float(t.get("stop_price", 0) or 0),
        "tp1": float(t.get("tp1_price", 0) or 0),
        "runner": float(t.get("runner_max_price", 0) or 0),
        "current_sl": float(t.get("current_sl", 0) or 0),
        "entry_time": datetime.fromtimestamp(entry_ts / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        ) if entry_ts else "—",
    }


def _get_open_trade_unrealized_pnl(open_trades: list[dict]) -> float:
    """Fetch current unrealized PnL from Bybit for the open live position."""
    if not open_trades:
        return 0.0
    try:
        pos = client.get_position(SYMBOL)
        if pos and abs(float(pos.get("size", 0) or 0)) > 0:
            return float(pos.get("unrealisedPnl", 0) or 0)
    except Exception as exc:
        app.logger.warning("Could not fetch open position unrealized PnL: %s", exc)
    return 0.0


def _load_open_live_trades() -> list[dict]:
    """Load open live trades from the bot's meta store."""
    raw = store.get_meta("open_live_trades", "")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _filter_one_at_a_time(trades: list[dict]) -> list[dict]:
    """Return trades filtered so only one position is open at a time.

    Trades are kept in chronological order; a trade is skipped if it starts
    before the previously kept trade has closed.
    """
    sorted_trades = sorted(
        trades,
        key=lambda t: int(t.get("entry_ts", 0) or t.get("exit_ts", 0) or 0),
    )
    filtered: list[dict] = []
    last_exit = 0
    for t in sorted_trades:
        entry = int(t.get("entry_ts", 0) or t.get("exit_ts", 0) or 0)
        exit_ = int(t.get("exit_ts", 0) or 0)
        if entry == 0 or exit_ == 0:
            continue
        if entry >= last_exit:
            filtered.append(t)
            last_exit = exit_
    return filtered


def _recompute_backtest_one_at_a_time(data: dict) -> dict:
    """Recompute backtest KPIs/equity assuming only one trade at a time."""
    capital = float(data.get("capital", TRADING_CAPITAL))
    all_trades = data.get("trades", [])
    trades = _filter_one_at_a_time(all_trades)

    if not trades:
        return {
            "label": "Tight - one at a time",
            "color": "#f7c566",
            "kpis": {},
            "equity": {"ts": [], "values": []},
            "drawdown": {"ts": [], "values": []},
            "static_drawdown": {"ts": [], "values": []},
            "trades": [],
        }

    # Build equity curve: start capital at first entry, then update at each exit
    equity_points: list[list] = []
    cum = capital
    first_entry = int(trades[0].get("entry_ts", 0) or trades[0].get("exit_ts", 0) or 0)
    if first_entry:
        equity_points.append([first_entry // 1000, cum])
    for t in trades:
        cum += float(t.get("pnl", 0) or 0)
        exit_ts = int(t.get("exit_ts", 0) or 0)
        if exit_ts:
            equity_points.append([exit_ts // 1000, cum])

    kpis = store.compute_kpis(trades, equity_points, capital)
    kpis["total_account_equity"] = kpis["final_equity"]
    kpis["allocated_capital"] = capital
    kpis["return_on_total_equity"] = kpis["total_return"]

    ts_list = [p[0] for p in equity_points]
    values = [p[1] for p in equity_points]

    return {
        "label": "Tight - one at a time",
        "color": "#f7c566",
        "kpis": kpis,
        "equity": {"ts": ts_list, "values": values},
        "drawdown": _drawdown_from_equity(ts_list, values),
        "static_drawdown": _static_drawdown_from_equity(ts_list, values, capital),
        "trades": [_format_trade(t) for t in trades[-50:][::-1]],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("fvg_dashboard.html")


@app.route("/api/backtest")
def api_backtest():
    """Return tight-variant backtest results."""
    data = _load_backtest_json()
    if data is None:
        return jsonify({"error": "backtest data not found"}), 404

    eq = data.get("equity", {"ts": [], "values": []})
    capital = float(data.get("capital", TRADING_CAPITAL))
    return jsonify(
        {
            "tight": {
                "label": "Tight (0.4% stop / 0.4% TP1)",
                "color": "#5b8cff",
                "kpis": data.get("kpis", {}),
                "equity": eq,
                "drawdown": _drawdown_from_equity(eq.get("ts", []), eq.get("values", [])),
                "static_drawdown": _static_drawdown_from_equity(
                    eq.get("ts", []), eq.get("values", []), capital
                ),
                "trades": [_format_trade(t) for t in data.get("trades", [])[-50:][::-1]],
            },
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    )


@app.route("/api/backtest/one_at_a_time")
def api_backtest_one_at_a_time():
    """Return tight-variant backtest results with one-trade-at-a-time filtering."""
    data = _load_backtest_json()
    if data is None:
        return jsonify({"error": "backtest data not found"}), 404

    return jsonify(
        {
            "tight": _recompute_backtest_one_at_a_time(data),
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    )


@app.route("/api/paper")
def api_paper():
    """Return live paper results for the tight variant."""
    total_equity, allocated = _get_account_split()

    trades = store.load_trades_variant("tight")
    equity_points = store.load_equity_curve_variant("tight")

    # Seed the curve with the live allocated capital if empty so the dashboard
    # still renders before the first paper trade closes.
    if not equity_points:
        equity_points = [[int(datetime.now(timezone.utc).timestamp()), allocated]]

    kpis = store.compute_kpis(trades, equity_points, allocated)
    # Show the real Bybit account split; paper PnL does not affect live equity
    kpis["total_account_equity"] = total_equity
    kpis["allocated_capital"] = allocated
    kpis["return_on_total_equity"] = 0.0

    ts_list = [p[0] for p in equity_points]
    values = [p[1] for p in equity_points]

    # Include open live trades so they appear in the Forward Test trade log
    open_live_trades = _load_open_live_trades()
    open_unrealized_pnl = _get_open_trade_unrealized_pnl(open_live_trades)
    open_live = [_format_open_trade(t, open_unrealized_pnl) for t in open_live_trades]

    return jsonify(
        {
            "variant": "tight",
            "kpis": kpis,
            "equity": {"ts": ts_list, "values": values},
            "drawdown": _drawdown_from_equity(ts_list, values),
            "static_drawdown": _static_drawdown_from_equity(ts_list, values, allocated),
            "trades": open_live + [_format_trade(t) for t in trades[-50:][::-1]],
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    )


if __name__ == "__main__":
    store.init()
    app.run(host="127.0.0.1", port=5556, threaded=True)
