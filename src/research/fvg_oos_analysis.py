"""
Out-of-sample validation + parameter sensitivity for SSRN-6725880 FVG on BTCUSDT.

- In-sample:  2020-03-25 to 2023-12-31
- Out-of-sample: 2024-01-01 to 2026-09-25
- Fixed-dollar risk per trade (= 0.5% of initial allocated capital).
- Parameter sensitivity: +/- 20% around baseline for key parameters.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")
from src.shared import config
from src.fvg.backtest import compute_metrics, run_backtest

sys.path.insert(0, str(config.PROJECT_ROOT))
from src.shared import store

HISTORY_DIR = Path(r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_m15_max\data")

# Use the same capital split as the live bot will use
TOTAL_EQUITY = 184_906.08  # placeholder; will be overwritten by Bybit fetch if available
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
    "direction_filter": None,
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
         capital: float, fixed_risk: float) -> dict:
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
    )
    equity_df = result["equity"]
    trades_df = result["trades"]
    metrics = compute_metrics(equity_df, trades_df, capital)
    return {
        "variant": variant,
        "params": params,
        "trades": len(trades_df),
        **metrics,
    }


def _split_data(df5: pd.DataFrame, df1h: pd.DataFrame):
    is_mask = df5["timestamp"] < "2024-01-01"
    oos_mask = df5["timestamp"] >= "2024-01-01"
    df5_is = df5[is_mask].copy().reset_index(drop=True)
    df5_oos = df5[oos_mask].copy().reset_index(drop=True)
    df1h_is = df1h[df1h["timestamp"] < "2024-01-01"].copy().reset_index(drop=True)
    df1h_oos = df1h[df1h["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
    return df5_is, df5_oos, df1h_is, df1h_oos


def _sensitivity_grid(base_params: dict) -> list[dict]:
    """Generate +/- 20% variations for each tunable parameter."""
    keys = ["stop_pct", "tp1_pct", "runner_a_pct", "runner_a_plus_pct", "htf_proximity_pct"]
    grids = []
    for key in keys:
        base = base_params[key]
        for mult in [0.8, 1.0, 1.2]:
            p = dict(base_params)
            p[key] = round(base * mult, 6)
            grids.append({"label": f"{key}={p[key]:.4f}", "params": p})
    return grids


def main():
    df5 = _load_parquet("BTCUSDT", 15)
    df1h = _load_parquet("BTCUSDT", 60)
    print(f"Loaded M15: {df5.shape}, H1: {df1h.shape}")

    df5_is, df5_oos, df1h_is, df1h_oos = _split_data(df5, df1h)
    print(f"IS bars: {len(df5_is)} ({df5_is['timestamp'].iloc[0]} to {df5_is['timestamp'].iloc[-1]})")
    print(f"OOS bars: {len(df5_oos)} ({df5_oos['timestamp'].iloc[0]} to {df5_oos['timestamp'].iloc[-1]})")

    rows = []

    # 1) Baseline IS / OOS for both variants
    print("\n=== Baseline IS/OOS ===")
    for variant, params in BASELINE_VARIANTS.items():
        for label, df5_period, df1h_period in [
            ("IS", df5_is, df1h_is),
            ("OOS", df5_oos, df1h_oos),
        ]:
            r = _run(df5_period, df1h_period, variant, params, CAPITAL_PER_VARIANT, FIXED_RISK_AMOUNT)
            r["period"] = label
            r["test"] = "baseline"
            rows.append(r)
            print(f"{variant} {label}: trades={r['trades']} final=${r['final_equity']:,.2f} "
                  f"ret={r['total_return_pct']:.1f}% sharpe={r['sharpe']:.2f} mdd={r['max_drawdown_pct']:.2f}%")

    # 2) Parameter sensitivity on OOS only
    print("\n=== Parameter sensitivity (OOS) ===")
    for variant, base_params in BASELINE_VARIANTS.items():
        for g in _sensitivity_grid(base_params):
            r = _run(df5_oos, df1h_oos, variant, g["params"], CAPITAL_PER_VARIANT, FIXED_RISK_AMOUNT)
            r["period"] = "OOS"
            r["test"] = f"sens_{variant}_{g['label']}"
            rows.append(r)
            print(f"{variant} {g['label']}: trades={r['trades']} final=${r['final_equity']:,.2f} "
                  f"ret={r['total_return_pct']:.1f}% sharpe={r['sharpe']:.2f}")

    # Save results
    out_dir = config.PROJECT_ROOT / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "fvg_oos_sensitivity_results.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nSaved results to {out_path}")

    # Print summary tables
    df = pd.DataFrame(rows)
    print("\n=== BASELINE SUMMARY ===")
    print(df[df["test"] == "baseline"][["variant", "period", "trades", "final_equity",
                                          "total_return_pct", "cagr_pct", "sharpe",
                                          "max_drawdown_pct", "win_rate_pct", "profit_factor"]]
          .to_string(index=False))

    print("\n=== OOS SENSITIVITY SUMMARY (top by Sharpe) ===")
    sens = df[df["period"] == "OOS"].sort_values("sharpe", ascending=False)
    print(sens[["test", "trades", "final_equity", "total_return_pct", "sharpe",
                "max_drawdown_pct", "win_rate_pct", "profit_factor"]].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
