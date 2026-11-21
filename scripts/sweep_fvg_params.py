"""Parameter sweep for the FVG momentum strategy with realistic limit fills.

Loads cached M15/H1 data and runs the backtest engine across a grid of
parameter combinations. Reports standard KPIs for each combination.
"""
from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.fvg.backtest import compute_metrics, run_backtest
from src.fvg.strategy import Direction

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "research_results"

CAPITAL = 92_453.04
RISK_PER_TRADE_PCT = 0.005
USE_FIXED_RISK = True
DAILY_LOSS_CAP_MULTIPLIER = 2.0

# Default tight-variant parameters
DEFAULT_VARIANT = {
    "fvg_lookforward": 8,
    "stop_pct": 0.004,
    "tp1_pct": 0.004,
    "runner_a_pct": 0.020,
    "runner_a_plus_pct": 0.040,
    "htf_proximity_pct": 0.003,
}

COMMON_PARAMS = {
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "direction_filter": None,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}


def load_cache() -> tuple[pd.DataFrame, pd.DataFrame]:
    m15 = pd.read_pickle(DATA_DIR / "fvg_backtest_m15.pkl")
    h1 = pd.read_pickle(DATA_DIR / "fvg_backtest_h1.pkl")
    return m15, h1


def run_one(m15, h1, params: dict, label: str) -> dict:
    fixed_risk_amount = CAPITAL * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None
    fixed_daily_loss_cap = (
        fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else CAPITAL * 0.003
    )

    result = run_backtest(
        m15,
        h1,
        capital=CAPITAL,
        risk_per_trade_pct=None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=0.00055,
        exit_cost_pct=0.00055,
        **COMMON_PARAMS,
        **params,
    )
    trades_df = result["trades"]
    equity_df = result["equity"]
    metrics = compute_metrics(equity_df, trades_df, CAPITAL)
    if "error" in metrics:
        metrics = {k: None for k in [
            "start_capital", "final_equity", "total_return_pct", "cagr_pct",
            "ann_vol_pct", "sharpe", "max_drawdown_pct", "num_trades",
            "win_rate_pct", "profit_factor", "payoff_ratio", "avg_trade_pnl",
            "avg_r", "total_cost",
        ]}
    metrics["label"] = label
    metrics["params"] = json.dumps({**COMMON_PARAMS, **params}, default=str)
    return metrics


def sweep_lookforward(m15, h1):
    print("\n=== Sweep: fvg_lookforward ===")
    results = []
    for lf in [4, 6, 8, 12, 16, 24, 32, 48]:
        params = DEFAULT_VARIANT.copy()
        params["fvg_lookforward"] = lf
        label = f"lookforward_{lf}"
        results.append(run_one(m15, h1, params, label))
    return pd.DataFrame(results)


def sweep_stop_tp(m15, h1):
    print("\n=== Sweep: stop_pct / tp1_pct ===")
    results = []
    for stop_tp in [0.002, 0.003, 0.004, 0.005, 0.006, 0.008, 0.010]:
        params = DEFAULT_VARIANT.copy()
        params["stop_pct"] = stop_tp
        params["tp1_pct"] = stop_tp
        label = f"stop_tp_{stop_tp:.4f}"
        results.append(run_one(m15, h1, params, label))
    return pd.DataFrame(results)


def sweep_htf(m15, h1):
    print("\n=== Sweep: htf_proximity_pct ===")
    results = []
    for htf in [0.001, 0.002, 0.003, 0.005, 0.010, 0.020, 0.050, 0.100]:
        params = DEFAULT_VARIANT.copy()
        params["htf_proximity_pct"] = htf
        label = f"htf_{htf:.4f}"
        results.append(run_one(m15, h1, params, label))
    return pd.DataFrame(results)


def sweep_session(m15, h1):
    print("\n=== Sweep: session filter ===")
    results = []
    for session in [None, "Asia", "London", "NewYork", "Overlap"]:
        params = DEFAULT_VARIANT.copy()
        params["session_filter"] = session
        label = f"session_{session or 'all'}"
        results.append(run_one(m15, h1, params, label))
    return pd.DataFrame(results)


def _direction_value(direction: str | None):
    if direction == "long_only":
        return Direction.LONG
    if direction == "short_only":
        return Direction.SHORT
    return None


def sweep_direction(m15, h1):
    print("\n=== Sweep: direction filter ===")
    results = []
    for direction in [None, "long_only", "short_only"]:
        params = DEFAULT_VARIANT.copy()
        params["direction_filter"] = _direction_value(direction)
        label = f"direction_{direction or 'both'}"
        results.append(run_one(m15, h1, params, label))
    return pd.DataFrame(results)


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    m15, h1 = load_cache()
    print(f"Loaded M15: {len(m15)} rows, {m15['timestamp'].iloc[0]} -> {m15['timestamp'].iloc[-1]}")
    print(f"Loaded H1:  {len(h1)} rows, {h1['timestamp'].iloc[0]} -> {h1['timestamp'].iloc[-1]}")

    all_results = []
    all_results.append(sweep_lookforward(m15, h1))
    all_results.append(sweep_stop_tp(m15, h1))
    all_results.append(sweep_htf(m15, h1))
    all_results.append(sweep_session(m15, h1))
    all_results.append(sweep_direction(m15, h1))

    combined = pd.concat(all_results, ignore_index=True)
    cols = [
        "label", "num_trades", "final_equity", "total_return_pct", "cagr_pct",
        "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor",
        "payoff_ratio", "avg_trade_pnl", "avg_r", "total_cost", "params",
    ]
    combined = combined[[c for c in cols if c in combined.columns]]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"fvg_param_sweep_{timestamp}.csv"
    combined.to_csv(out_path, index=False)
    print(f"\nSaved results to {out_path}")

    print("\n=== Top 10 by Sharpe ===")
    print(combined.dropna(subset=["sharpe"]).sort_values("sharpe", ascending=False).head(10).to_string(index=False))

    print("\n=== Top 10 by final equity ===")
    print(combined.dropna(subset=["final_equity"]).sort_values("final_equity", ascending=False).head(10).to_string(index=False))


if __name__ == "__main__":
    main()
