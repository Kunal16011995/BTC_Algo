"""
Focused parameter + direction optimization for SSRN-6725880 FVG on BTCUSDT OOS.

Phase 1: Compare long-only / short-only / both with baseline params.
Phase 2: Grid-search stop_pct / tp1_pct around baseline for promising directions.

All tests use fixed-dollar risk and hybrid costs on the OOS period.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")
from backtest import compute_metrics, run_backtest
from strategy import Direction

HISTORY_DIR = Path(r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_m15_max\data")

# Match live bot allocation
TOTAL_EQUITY = 184_906.08
CAPITAL_PER_VARIANT = TOTAL_EQUITY / 2
FIXED_RISK_AMOUNT = CAPITAL_PER_VARIANT * 0.005

BASELINE_VARIANTS = {
    "base": {
        "stop_pct": 0.005,
        "tp1_pct": 0.005,
        "runner_a_pct": 0.020,
        "runner_a_plus_pct": 0.040,
        "htf_proximity_pct": 0.003,
    },
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
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}


def _load_parquet(symbol: str, minutes: int) -> pd.DataFrame:
    path = HISTORY_DIR / f"{symbol}_{minutes}.parquet"
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    return df


def _run(df5: pd.DataFrame, df1h: pd.DataFrame, variant: str, params: dict,
         direction_filter, capital: float, fixed_risk: float) -> dict:
    result = run_backtest(
        df5,
        df1h,
        capital=capital,
        risk_per_trade_pct=None,
        fixed_risk_amount=fixed_risk,
        daily_loss_cap_pct=0.003,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **params,
        direction_filter=direction_filter,
    )
    equity_df = result["equity"]
    trades_df = result["trades"]
    if trades_df.empty:
        return None
    metrics = compute_metrics(equity_df, trades_df, capital)
    return {
        "variant": variant,
        "params": params,
        "direction": "both" if direction_filter is None else ("long" if direction_filter == Direction.LONG else "short"),
        "trades": len(trades_df),
        **metrics,
    }


def main():
    df5_full = _load_parquet("BTCUSDT", 15)
    df1h_full = _load_parquet("BTCUSDT", 60)

    # OOS data only
    df5 = df5_full[df5_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
    df1h = df1h_full[df1h_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
    print(f"OOS M15: {len(df5)} bars ({df5['timestamp'].iloc[0]} to {df5['timestamp'].iloc[-1]})")

    rows = []

    # Phase 1: direction comparison with baseline params
    print("\n=== Phase 1: direction comparison (baseline params) ===")
    for variant, params in BASELINE_VARIANTS.items():
        for direction_filter, label in [(None, "both"), (Direction.LONG, "long"), (Direction.SHORT, "short")]:
            r = _run(df5, df1h, variant, params, direction_filter, CAPITAL_PER_VARIANT, FIXED_RISK_AMOUNT)
            if r is None:
                print(f"{variant} {label}: no trades")
                continue
            r["phase"] = "direction"
            rows.append(r)
            print(f"{variant} {label}: trades={r['trades']} final=${r['final_equity']:,.2f} "
                  f"ret={r['total_return_pct']:.1f}% sharpe={r['sharpe']:.2f} mdd={r['max_drawdown_pct']:.2f}% "
                  f"wr={r['win_rate_pct']:.1f}%")

    # Phase 2: grid around stop_pct / tp1_pct for each variant+direction that is not worse than baseline
    print("\n=== Phase 2: stop/tp grid ===")
    for variant, base_params in BASELINE_VARIANTS.items():
        for direction_filter, label in [(None, "both"), (Direction.LONG, "long"), (Direction.SHORT, "short")]:
            for stop_pct in [0.003, 0.004, 0.005, 0.006]:
                for tp1_pct in [0.003, 0.004, 0.005, 0.006]:
                    params = dict(base_params)
                    params["stop_pct"] = stop_pct
                    params["tp1_pct"] = tp1_pct
                    r = _run(df5, df1h, variant, params, direction_filter, CAPITAL_PER_VARIANT, FIXED_RISK_AMOUNT)
                    if r is None:
                        continue
                    r["phase"] = "grid"
                    rows.append(r)

    # Save
    out_path = Path(__file__).resolve().parent / "data" / "fvg_optimization_results.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nSaved {len(rows)} results to {out_path}")

    # Summaries
    df = pd.DataFrame(rows)

    print("\n=== TOP 10 BY SHARPE (all tests) ===")
    top = df.sort_values("sharpe", ascending=False).head(10)
    print(top[["variant", "direction", "trades", "final_equity", "total_return_pct",
               "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor"]].to_string(index=False))

    print("\n=== TOP 10 BY FINAL EQUITY (all tests) ===")
    top = df.sort_values("final_equity", ascending=False).head(10)
    print(top[["variant", "direction", "trades", "final_equity", "total_return_pct",
               "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor"]].to_string(index=False))


if __name__ == "__main__":
    main()
