"""
Faster focused optimization for SSRN-6725880 FVG on BTCUSDT OOS.

- Phase 1: direction comparison (both / long / short) with baseline params.
- Phase 2: small 2x2 grid around baseline stop_pct / tp1_pct for best direction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")
from src.fvg.backtest import compute_metrics, run_backtest
from src.fvg.strategy import Direction

HISTORY_DIR = Path(r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_m15_max\data")

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


def _run(df5, df1h, variant, params, direction_filter, label):
    result = run_backtest(
        df5, df1h,
        capital=CAPITAL_PER_VARIANT,
        risk_per_trade_pct=None,
        fixed_risk_amount=FIXED_RISK_AMOUNT,
        daily_loss_cap_pct=0.003,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **params,
        direction_filter=direction_filter,
    )
    trades_df = result["trades"]
    if trades_df.empty:
        print(f"{variant} {label}: no trades")
        return None
    metrics = compute_metrics(result["equity"], trades_df, CAPITAL_PER_VARIANT)
    return {
        "variant": variant,
        "direction": label,
        "params": params,
        "trades": len(trades_df),
        **metrics,
    }


def main():
    df5_full = _load_parquet("BTCUSDT", 15)
    df1h_full = _load_parquet("BTCUSDT", 60)
    df5 = df5_full[df5_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
    df1h = df1h_full[df1h_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)
    print(f"OOS M15: {len(df5)} bars")

    rows = []

    # Phase 1: direction comparison
    print("\n=== Phase 1: direction comparison ===")
    for variant, params in BASELINE_VARIANTS.items():
        for direction_filter, label in [(None, "both"), (Direction.LONG, "long"), (Direction.SHORT, "short")]:
            r = _run(df5, df1h, variant, params, direction_filter, label)
            if r:
                r["phase"] = "direction"
                rows.append(r)

    # Save phase 1 immediately
    out_path = Path(__file__).resolve().parent.parent / "data" / "fvg_optimization_results.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)

    # Phase 2: small grid on best direction per variant
    print("\n=== Phase 2: small stop/tp grid ===")
    for variant, base_params in BASELINE_VARIANTS.items():
        # find best direction for this variant
        variant_rows = [r for r in rows if r["variant"] == variant and r.get("phase") == "direction"]
        if not variant_rows:
            continue
        best_dir_label = max(variant_rows, key=lambda x: x["sharpe"])["direction"]
        best_dir_filter = None if best_dir_label == "both" else (Direction.LONG if best_dir_label == "long" else Direction.SHORT)

        for stop_pct in [0.004, 0.005, 0.006]:
            for tp1_pct in [0.003, 0.004, 0.005]:
                params = dict(base_params)
                params["stop_pct"] = stop_pct
                params["tp1_pct"] = tp1_pct
                r = _run(df5, df1h, variant, params, best_dir_filter, best_dir_label)
                if r:
                    r["phase"] = "grid"
                    rows.append(r)

    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nSaved {len(rows)} results to {out_path}")

    df = pd.DataFrame(rows)
    print("\n=== TOP 10 BY SHARPE ===")
    print(df.sort_values("sharpe", ascending=False)
          [["variant", "direction", "trades", "final_equity", "total_return_pct",
            "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor"]].head(10).to_string(index=False))

    print("\n=== TOP 10 BY FINAL EQUITY ===")
    print(df.sort_values("final_equity", ascending=False)
          [["variant", "direction", "trades", "final_equity", "total_return_pct",
            "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
