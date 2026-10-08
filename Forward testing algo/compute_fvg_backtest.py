"""
Pre-compute SSRN-6725880 FVG backtest results for the dashboard Backtest tab.

Runs the single tight variant over the full BTCUSDT M15 history stored in
`ssrn-6725880_btc_m15_max/data/` using the hybrid cost assumption
(0.01% entry maker / 0.06% exit taker). Capital is fixed at the Bybit trading
allocation ($92,453.04) and is no longer split.

Outputs:
    data/fvg_backtest_tight.json
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Strategy engine path
sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")

from backtest import compute_metrics, run_backtest

# Import local store for consistent KPI computation
import store

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_DIR = Path(r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_m15_max\data")

TRADING_CAPITAL = 92_453.04
RISK_PER_TRADE_PCT = 0.005  # only used to derive fixed risk amount
USE_FIXED_RISK = True  # True = fixed dollar risk per trade, False = % of current equity
DAILY_LOSS_CAP_MULTIPLIER = 2.0  # daily cap = N x fixed risk per trade

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


def _load_parquet(symbol: str, minutes: int) -> pd.DataFrame:
    path = HISTORY_DIR / f"{symbol}_{minutes}.parquet"
    df = pd.read_parquet(path)
    # Ensure lowercase columns and UTC timestamps
    df.columns = [c.lower() for c in df.columns]
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    return df


def _get_trading_capital() -> float:
    """Return the fixed trading capital for the single tight variant.

    Allow env TOTAL_EQUITY to override for offline testing, but the strategy
    always trades one fixed allocation.
    """
    load_dotenv(BASE_DIR / ".env")
    env_equity = os.getenv("TOTAL_EQUITY")
    if env_equity:
        return float(env_equity)
    return TRADING_CAPITAL


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


def _run_variant(variant: str, params: dict, df5: pd.DataFrame, df1h: pd.DataFrame, capital: float):
    print(f"Running backtest for variant: {variant} ...")
    t0 = time.time()

    fixed_risk_amount = capital * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None
    fixed_daily_loss_cap = fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else capital * 0.003
    result = run_backtest(
        df5,
        df1h,
        capital=capital,
        risk_per_trade_pct=None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=0.00055,
        exit_cost_pct=0.00055,
        **COMMON_PARAMS,
        **params,
    )

    equity_df = result["equity"]
    trades_df = result["trades"]

    # Convert equity curve to (ts_seconds, equity) points
    equity_points = [
        [int(ts.timestamp()), float(eq)]
        for ts, eq in zip(equity_df["timestamp"], equity_df["equity"])
    ]

    # Convert trades to dashboard-compatible dicts
    trades = [_build_trade_dict(row, variant) for _, row in trades_df.iterrows()]

    # Compute KPIs using the same function the dashboard uses
    kpis = store.compute_kpis(trades, equity_points, capital)
    # Account basis equals trading basis for this virtual test
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
    print(f"  -> {len(trades)} trades, final equity ${kpis['final_equity']:.2f}, "
          f"Sharpe {kpis['sharpe_ratio']:.2f}, max DD {kpis['max_drawdown']:.2f}% "
          f"({elapsed:.1f}s)")
    print(f"  -> saved {out_path}")
    return payload


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    store.init()

    capital = _get_trading_capital()
    print(f"Trading capital for single tight variant: ${capital:,.2f}")

    df5 = _load_parquet("BTCUSDT", 15)
    df1h = _load_parquet("BTCUSDT", 60)
    print(f"Loaded M15: {df5.shape}, H1: {df1h.shape}")

    for variant, params in VARIANTS.items():
        _run_variant(variant, params, df5, df1h, capital)


if __name__ == "__main__":
    main()
