"""
Daily loss cap optimization for the chosen FVG config.

Tests daily cap = 1x, 1.5x, 2x, 2.5x, 3x, 3.5x, 4x the fixed per-trade risk,
across IS, OOS, and total data.

Chosen config: tight variant, both directions, fixed-dollar risk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")
from src.fvg.backtest import compute_metrics, run_backtest

HISTORY_DIR = Path(r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_m15_max\data")

TOTAL_EQUITY = 184_906.08
CAPITAL_PER_VARIANT = TOTAL_EQUITY / 2
FIXED_RISK_AMOUNT = CAPITAL_PER_VARIANT * 0.005

CHOSEN_PARAMS = {
    "stop_pct": 0.004,
    "tp1_pct": 0.004,
    "runner_a_pct": 0.020,
    "runner_a_plus_pct": 0.040,
    "htf_proximity_pct": 0.003,
}

COMMON_PARAMS = {
    "fvg_lookforward": 8,
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "direction_filter": None,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}

CAP_MULTIPLIERS = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]


def _load_parquet(symbol: str, minutes: int) -> pd.DataFrame:
    path = HISTORY_DIR / f"{symbol}_{minutes}.parquet"
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    return df


def _run(df5, df1h, period, cap_mult):
    fixed_cap = FIXED_RISK_AMOUNT * cap_mult
    result = run_backtest(
        df5, df1h,
        capital=CAPITAL_PER_VARIANT,
        risk_per_trade_pct=None,
        fixed_risk_amount=FIXED_RISK_AMOUNT,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_cap,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **CHOSEN_PARAMS,
    )
    equity_df = result["equity"]
    trades_df = result["trades"]
    if trades_df.empty:
        return None
    metrics = compute_metrics(equity_df, trades_df, CAPITAL_PER_VARIANT)
    return {
        "period": period,
        "cap_mult": cap_mult,
        "fixed_cap": fixed_cap,
        "trades": len(trades_df),
        **metrics,
    }


def main():
    df5_full = _load_parquet("BTCUSDT", 15)
    df1h_full = _load_parquet("BTCUSDT", 60)

    df5_is = df5_full[df5_full["timestamp"] < "2024-01-01"].copy().reset_index(drop=True)
    df5_oos = df5_full[df5_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
    df1h_is = df1h_full[df1h_full["timestamp"] < "2024-01-01"].copy().reset_index(drop=True)
    df1h_oos = df1h_full[df1h_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)

    rows = []
    for cap_mult in CAP_MULTIPLIERS:
        for period, df5, df1h in [
            ("IS", df5_is, df1h_is),
            ("OOS", df5_oos, df1h_oos),
            ("Total", df5_full, df1h_full),
        ]:
            r = _run(df5, df1h, period, cap_mult)
            if r:
                rows.append(r)
                print(f"cap={cap_mult}x risk (${r['fixed_cap']:.2f}) {period}: "
                      f"trades={r['trades']} final=${r['final_equity']:,.2f} "
                      f"ret={r['total_return_pct']:.1f}% sharpe={r['sharpe']:.2f} "
                      f"mdd={r['max_drawdown_pct']:.2f}%")

    out_path = Path(__file__).resolve().parent.parent / "data" / "fvg_daily_cap_optimization.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)

    df = pd.DataFrame(rows)
    print("\n=== SUMMARY BY CAP MULTIPLIER ===")
    print(df[["period", "cap_mult", "fixed_cap", "trades", "final_equity",
              "total_return_pct", "cagr_pct", "sharpe", "max_drawdown_pct",
              "win_rate_pct", "profit_factor"]].to_string(index=False))

    print("\n=== TOP 10 BY SHARPE ===")
    print(df.sort_values("sharpe", ascending=False)
          [["period", "cap_mult", "fixed_cap", "trades", "final_equity",
            "total_return_pct", "sharpe", "max_drawdown_pct"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
