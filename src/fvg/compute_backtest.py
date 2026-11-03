"""
Pre-compute SSRN-6725880 FVG backtest results for the dashboard Backtest tab.

Fetches BTCUSDT M15 and H1 klines from Bybit, runs the tight variant using the
same backtest engine as the GitHub repo, and writes:

    data/fvg_backtest_tight.json

Run with:
    python compute_fvg_backtest.py
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared import store
from src.fvg.backtest import compute_metrics, run_backtest
from src.shared.bybit_client import BybitClient

BASE_DIR = config.PROJECT_ROOT
DATA_DIR = BASE_DIR / "data"

TRADING_CAPITAL = 92_453.04
RISK_PER_TRADE_PCT = 0.005
USE_FIXED_RISK = True
DAILY_LOSS_CAP_MULTIPLIER = 2.0

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
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}


def _load_klines(client: BybitClient, symbol: str, interval: str,
                 start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    """Fetch klines from Bybit and return a sorted DataFrame."""
    rows = client.get_all_klines(symbol, interval, start_ms=start_ms, end_ms=end_ms)
    if not rows:
        raise RuntimeError(f"No {interval} klines returned from Bybit")

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _build_trade_dict(row: pd.Series, variant: str) -> dict:
    side = "Buy" if row["direction"] == "long" else "Sell"
    qty = row["size_usd"] / row["entry_price"] if row["entry_price"] else 0.0
    entry_ts_ms = int(row["entry_time"].timestamp() * 1000)
    exit_ts_ms = int(row["exit_time"].timestamp() * 1000)
    return {
        "id": f"{variant}-{uuid.uuid4().hex[:12]}",
        "variant": variant,
        "symbol": "BTCUSDT",
        "side": side,
        "qty": round(qty, 6),
        "entry_price": float(row["entry_price"]),
        "exit_price": float(row["exit_price"]),
        "pnl": float(row["net_pnl"]),
        "entry_ts": entry_ts_ms,
        "exit_ts": exit_ts_ms,
        "raw": {
            "grade": row["grade"],
            "gross_pnl": float(row["gross_pnl"]),
            "cost": float(row["cost"]),
            "result_r": float(row["result_r"]),
            "exit_reason": row["exit_reason"],
            "direction": row["direction"],
            "size_usd": float(row["size_usd"]),
        },
    }


def _run_variant(client: BybitClient, variant: str, params: dict, capital: float):
    print(f"Running backtest for variant: {variant} ...")
    t0 = time.time()

    # Fetch as much history as Bybit allows. Start from 2020-03-25; earlier
    # dates can cause Bybit public kline service errors.
    start_ms = int(datetime(2020, 3, 25, tzinfo=timezone.utc).timestamp() * 1000)

    df5 = _load_klines(client, "BTCUSDT", "15", start_ms=start_ms)
    df1h = _load_klines(client, "BTCUSDT", "60", start_ms=start_ms)

    # Drop the current forming bar so the backtest uses only completed bars,
    # matching the corrected live bot and avoiding ephemeral end-of-window setups.
    if len(df5) > 1:
        df5 = df5.iloc[:-1].copy().reset_index(drop=True)
    if len(df1h) > 1:
        df1h = df1h.iloc[:-1].copy().reset_index(drop=True)

    print(f"Loaded M15: {df5.shape}, from {df5['timestamp'].iloc[0]} to {df5['timestamp'].iloc[-1]}")
    print(f"Loaded H1:  {df1h.shape}, from {df1h['timestamp'].iloc[0]} to {df1h['timestamp'].iloc[-1]}")

    fixed_risk_amount = capital * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None
    fixed_daily_loss_cap = (
        fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else capital * 0.003
    )

    result = run_backtest(
        df5,
        df1h,
        capital=capital,
        risk_per_trade_pct=None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **params,
    )

    equity_df = result["equity"]
    trades_df = result["trades"]

    equity_points = [
        [int(ts.timestamp()), float(eq)]
        for ts, eq in zip(equity_df["timestamp"], equity_df["equity"])
    ]

    trades = [_build_trade_dict(row, variant) for _, row in trades_df.iterrows()]

    kpis = store.compute_kpis(trades, equity_points, capital)
    kpis["total_account_equity"] = kpis["final_equity"]
    kpis["allocated_capital"] = capital
    kpis["return_on_total_equity"] = kpis["total_return"]

    metrics = compute_metrics(equity_df, trades_df, capital)

    payload = {
        "variant": variant,
        "params": {**COMMON_PARAMS, **params},
        "capital": capital,
        "total_account_equity": capital,
        "risk_per_trade_pct": None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        "fixed_risk_amount": fixed_risk_amount if USE_FIXED_RISK else None,
        "daily_loss_cap_pct": None,
        "fixed_daily_loss_cap": fixed_daily_loss_cap,
        "daily_loss_cap_multiplier": DAILY_LOSS_CAP_MULTIPLIER,
        "kpis": kpis,
        "metrics": metrics,
        "equity": {"ts": [p[0] for p in equity_points], "values": [p[1] for p in equity_points]},
        "trades": trades,
    }

    out_path = DATA_DIR / f"fvg_backtest_{variant}.json"
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    elapsed = time.time() - t0
    print(
        f"  -> {len(trades)} trades, final equity ${kpis['final_equity']:.2f}, "
        f"Sharpe {kpis['sharpe_ratio']:.2f}, max DD {kpis['max_drawdown']:.2f}% "
        f"({elapsed:.1f}s)"
    )
    print(f"  -> saved {out_path}")
    return payload


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    store.init()

    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    capital = TRADING_CAPITAL
    print(f"Trading capital for single tight variant: ${capital:,.2f}")

    for variant, params in VARIANTS.items():
        _run_variant(client, variant, params, capital)


if __name__ == "__main__":
    main()
