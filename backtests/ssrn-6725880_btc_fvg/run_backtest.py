import os
import json
import datetime
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt

from strategy import generate_setups
from backtest import run_backtest, compute_metrics

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_DIR = Path(__file__).parent / "output"
CAPITAL = 10_000.0
RISK_PER_TRADE_PCT = 0.01  # 1% of capital per trade
DAILY_LOSS_CAP_PCT = 0.003  # 0.30% of capital daily loss cap

# Bybit BTCUSDT linear perpetual cost estimate
COST_PCT = 0.0016  # taker-style: 0.06% taker fee * 2 + 0.02% spread + 0.02% slippage

# Strategy parameters (percentage of price) — scaled for BTCUSDT volatility/costs.
# Ratios mirror the paper: 1:1 TP1, 3:1 A+ TP2, etc.
STRATEGY_PARAMS = {
    "stop_pct": 0.005,
    "tp1_pct": 0.005,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
    "runner_a_pct": 0.020,
    "runner_a_plus_pct": 0.040,
    "htf_proximity_pct": 0.003,
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
}

# In-sample / out-of-sample split
IS_END = pd.Timestamp("2024-06-30 23:59:59", tz="UTC")


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    df5 = pd.read_parquet(DATA_DIR / "BTCUSDT_5.parquet")
    df1h = pd.read_parquet(DATA_DIR / "BTCUSDT_60.parquet")
    return df5, df1h


def run_period(df5: pd.DataFrame, df1h: pd.DataFrame, name: str) -> dict:
    print(f"\n=== Running {name} backtest ===")
    result = run_backtest(
        df5,
        df1h,
        capital=CAPITAL,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
        daily_loss_cap_pct=DAILY_LOSS_CAP_PCT,
        cost_pct=COST_PCT,
        **STRATEGY_PARAMS,
    )
    metrics = compute_metrics(result["equity"], result["trades"], CAPITAL)
    print(f"Trades: {metrics['num_trades']}, Return: {metrics['total_return_pct']:.2f}%, "
          f"Sharpe: {metrics['sharpe']:.2f}, MaxDD: {metrics['max_drawdown_pct']:.2f}%, "
          f"WinRate: {metrics['win_rate_pct']:.1f}%")
    return {"result": result, "metrics": metrics, "name": name}


def plot_equityCurves(periods: list[dict], output_path: Path):
    fig, ax = plt.subplots(figsize=(12, 6))
    for p in periods:
        eq = p["result"]["equity"]
        ax.plot(eq["timestamp"], eq["equity"], label=p["name"])
    ax.axhline(CAPITAL, color="gray", linestyle="--", label="Start capital")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity (USD)")
    ax.set_title("SSRN-6725880 FVG Momentum Strategy — BTCUSDT")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    print(f"Saved equity curve to {output_path}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df5, df1h = load_data()

    # Split
    df5_is = df5[df5["timestamp"] <= IS_END].reset_index(drop=True)
    df5_oos = df5[df5["timestamp"] > IS_END].reset_index(drop=True)
    df1h_is = df1h[df1h["timestamp"] <= IS_END].reset_index(drop=True)
    df1h_oos = df1h[df1h["timestamp"] > IS_END].reset_index(drop=True)

    # Run periods
    full = run_period(df5, df1h, "Full")
    is_ = run_period(df5_is, df1h_is, "In-Sample")
    oos = run_period(df5_oos, df1h_oos, "Out-of-Sample")

    # Save outputs
    for p in [full, is_, oos]:
        name = p["name"].lower().replace("-", "_")
        p["result"]["equity"].to_parquet(OUTPUT_DIR / f"equity_{name}.parquet", index=False)
        if not p["result"]["trades"].empty:
            p["result"]["trades"].to_parquet(OUTPUT_DIR / f"trades_{name}.parquet", index=False)

    # Plot
    plot_equityCurves([full, is_, oos], OUTPUT_DIR / "equity_curve.jpg")

    # Summary report
    summary = {
        "strategy": "SSRN-6725880 A/A+ Momentum FVG",
        "instrument": "BTCUSDT",
        "capital": CAPITAL,
        "risk_per_trade_pct": RISK_PER_TRADE_PCT,
        "daily_loss_cap_pct": DAILY_LOSS_CAP_PCT,
        "cost_pct": COST_PCT,
        "strategy_params": STRATEGY_PARAMS,
        "is_end": IS_END.isoformat(),
        "periods": {
            p["name"]: p["metrics"] for p in [full, is_, oos]
        },
    }
    with open(OUTPUT_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nSaved summary to {OUTPUT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
