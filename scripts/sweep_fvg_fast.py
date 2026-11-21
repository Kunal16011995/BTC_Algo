"""Fast focused sweep for FVG limit-fill strategy.

Pre-generates setups for each fvg_lookforward value, then reuses them across
stop_pct/tp1_pct combinations to avoid redundant setup generation.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.fvg.backtest import compute_metrics, run_backtest, simulate_trade
from src.fvg.strategy import generate_setups

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "research_results"

CAPITAL = 92_453.04
RISK_PER_TRADE_PCT = 0.005
USE_FIXED_RISK = True
DAILY_LOSS_CAP_MULTIPLIER = 2.0

COMMON = {
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "direction_filter": None,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}

VARIANT = {
    "stop_pct": 0.004,
    "tp1_pct": 0.004,
    "runner_a_pct": 0.020,
    "runner_a_plus_pct": 0.040,
    "htf_proximity_pct": 0.003,
}


def load_cache():
    m15 = pd.read_pickle(DATA_DIR / "fvg_backtest_m15.pkl")
    h1 = pd.read_pickle(DATA_DIR / "fvg_backtest_h1.pkl")
    return m15, h1


def run_from_setups(m15, setups, stop_pct, tp1_pct, runner_a_pct, runner_a_plus_pct, fvg_lookforward, label):
    """Run backtest from a pre-generated list of Setup objects."""
    capital = CAPITAL
    equity = [capital]
    equity_time = [m15["timestamp"].iloc[0]]
    trades = []
    daily_pnl = {}

    fixed_risk_amount = capital * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None
    fixed_daily_loss_cap = fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else capital * 0.003

    for setup in setups:
        day = setup.timestamp.normalize()
        if daily_pnl.get(day, 0.0) <= -fixed_daily_loss_cap:
            continue

        mask = m15["timestamp"] == setup.timestamp
        if not mask.any():
            continue
        start_idx = int(mask.idxmax())

        # Override setup exit distances for this run
        from dataclasses import replace
        stop_dist = setup.entry_price * stop_pct
        tp1_dist = setup.entry_price * tp1_pct
        if setup.grade.value == "A+":
            runner_dist = setup.entry_price * runner_a_plus_pct
        else:
            runner_dist = setup.entry_price * runner_a_pct

        if setup.direction.name == "LONG":
            stop_price = setup.entry_price - stop_dist
            tp1_price = setup.entry_price + tp1_dist
            runner_max = setup.entry_price + runner_dist
        else:
            stop_price = setup.entry_price + stop_dist
            tp1_price = setup.entry_price - tp1_dist
            runner_max = setup.entry_price - runner_dist

        setup_run = replace(
            setup,
            stop_price=stop_price,
            tp1_price=tp1_price,
            runner_max_price=runner_max,
        )

        size_usd = fixed_risk_amount / (abs(setup_run.entry_price - stop_price) / setup_run.entry_price)

        trade = simulate_trade(
            setup_run, m15, start_idx, size_usd,
            entry_cost_pct=0.00055, exit_cost_pct=0.00055,
            fvg_lookforward=fvg_lookforward,
        )
        if trade is None:
            continue

        trades.append(trade)
        capital += trade.net_pnl
        equity.append(capital)
        equity_time.append(trade.exit_time)
        exit_day = trade.exit_time.normalize()
        daily_pnl[exit_day] = daily_pnl.get(exit_day, 0.0) + trade.net_pnl

    equity_df = pd.DataFrame({"timestamp": equity_time, "equity": equity})
    trades_df = pd.DataFrame([t.__dict__ for t in trades])
    metrics = compute_metrics(equity_df, trades_df, CAPITAL)
    if "error" in metrics:
        metrics = {k: None for k in [
            "start_capital", "final_equity", "total_return_pct", "cagr_pct",
            "ann_vol_pct", "sharpe", "max_drawdown_pct", "num_trades",
            "win_rate_pct", "profit_factor", "payoff_ratio", "avg_trade_pnl",
            "avg_r", "total_cost",
        ]}
    metrics["label"] = label
    metrics["params"] = json.dumps({"fvg_lookforward": fvg_lookforward, "stop_pct": stop_pct, "tp1_pct": tp1_pct}, default=str)
    return metrics


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    m15, h1 = load_cache()
    print(f"M15: {len(m15)} rows, {m15['timestamp'].iloc[0]} -> {m15['timestamp'].iloc[-1]}")

    lookforwards = [8, 16, 32, 64]
    stop_tps = [(0.004, 0.004), (0.008, 0.008), (0.012, 0.012)]

    results = []
    for lf in lookforwards:
        print(f"\nGenerating setups for fvg_lookforward={lf} ...")
        params = {**COMMON, **VARIANT, "fvg_lookforward": lf}
        setups = generate_setups(m15, h1, **params)
        print(f"  -> {len(setups)} setups")

        for stop_pct, tp1_pct in stop_tps:
            label = f"lf{lf}_stop{stop_pct:.4f}_tp{tp1_pct:.4f}"
            print(f"  Running {label} ...")
            metrics = run_from_setups(
                m15, setups, stop_pct, tp1_pct,
                VARIANT["runner_a_pct"], VARIANT["runner_a_plus_pct"],
                lf, label,
            )
            results.append(metrics)
            print(f"    -> trades={metrics['num_trades']}, equity={metrics['final_equity']:.2f}, sharpe={metrics['sharpe']:.3f}")

    df = pd.DataFrame(results)
    cols = ["label", "num_trades", "final_equity", "total_return_pct", "cagr_pct",
            "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor",
            "payoff_ratio", "avg_trade_pnl", "avg_r", "total_cost", "params"]
    df = df[[c for c in cols if c in df.columns]]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"fvg_fast_sweep_{timestamp}.csv"
    df.to_csv(out_path, index=False)
    print(f"\nSaved {out_path}")

    print("\n=== Top 10 by Sharpe ===")
    print(df.dropna(subset=["sharpe"]).sort_values("sharpe", ascending=False).head(10).to_string(index=False))

    print("\n=== Top 10 by final equity ===")
    print(df.dropna(subset=["final_equity"]).sort_values("final_equity", ascending=False).head(10).to_string(index=False))


if __name__ == "__main__":
    main()
