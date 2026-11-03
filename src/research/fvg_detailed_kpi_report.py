"""
Detailed KPI report for the chosen optimized FVG config across IS / OOS / Total.

Chosen config: tight variant, both directions, baseline params
(stop_pct=0.004, tp1_pct=0.004, runner=0.04, htf_proximity=0.003)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")
from src.shared import config
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


def _load_parquet(symbol: str, minutes: int) -> pd.DataFrame:
    path = HISTORY_DIR / f"{symbol}_{minutes}.parquet"
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    return df


def _max_consecutive(series: pd.Series, value: bool) -> int:
    """Max consecutive True/False in a boolean series."""
    max_streak = streak = 0
    for v in series:
        if v == value:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def _run_period(df5: pd.DataFrame, df1h: pd.DataFrame, label: str) -> dict:
    result = run_backtest(
        df5,
        df1h,
        capital=CAPITAL_PER_VARIANT,
        risk_per_trade_pct=None,
        fixed_risk_amount=FIXED_RISK_AMOUNT,
        daily_loss_cap_pct=0.003,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **CHOSEN_PARAMS,
    )
    equity_df = result["equity"]
    trades_df = result["trades"]
    metrics = compute_metrics(equity_df, trades_df, CAPITAL_PER_VARIANT)

    if trades_df.empty:
        return {"label": label, "error": "No trades"}

    # Enrich trade data
    trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_time"])
    trades_df["day"] = trades_df["exit_dt"].dt.date
    trades_df["is_win"] = trades_df["net_pnl"] > 0

    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]

    # R-multiples
    risk_per_trade = FIXED_RISK_AMOUNT
    trades_df["r_multiple"] = trades_df["net_pnl"] / risk_per_trade

    # Daily PnL
    daily_pnl = trades_df.groupby("day")["net_pnl"].sum()

    # Equity curve derived metrics
    eq = equity_df.set_index("timestamp")["equity"]
    peak = eq.cummax()
    dd = (eq - peak) / peak
    dd_dur = (dd < 0).astype(int).groupby((dd == 0).astype(int).cumsum()).cumsum()

    report = {
        "label": label,
        "start_date": str(df5["timestamp"].iloc[0]),
        "end_date": str(df5["timestamp"].iloc[-1]),
        "initial_equity": CAPITAL_PER_VARIANT,
        "final_equity": metrics["final_equity"],
        "total_return_pct": metrics["total_return_pct"],
        "cagr_pct": metrics["cagr_pct"],
        "ann_vol_pct": metrics["ann_vol_pct"],
        "sharpe": metrics["sharpe"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "num_trades": len(trades_df),
        "win_rate_pct": metrics["win_rate_pct"],
        "loss_rate_pct": 100 - metrics["win_rate_pct"],
        "profit_factor": metrics["profit_factor"],
        "payoff_ratio": metrics["payoff_ratio"],
        "avg_trade_pnl": metrics["avg_trade_pnl"],
        "avg_r": metrics["avg_r"],
        "total_cost": metrics["total_cost"],
        "gross_pnl": trades_df["gross_pnl"].sum(),
        "net_pnl": trades_df["net_pnl"].sum(),
        # Win/loss details
        "num_wins": len(wins),
        "num_losses": len(losses),
        "avg_win": wins["net_pnl"].mean() if len(wins) > 0 else 0,
        "avg_loss": losses["net_pnl"].mean() if len(losses) > 0 else 0,
        "max_win": trades_df["net_pnl"].max(),
        "max_loss": trades_df["net_pnl"].min(),
        "best_r": trades_df["r_multiple"].max(),
        "worst_r": trades_df["r_multiple"].min(),
        "avg_win_r": (wins["net_pnl"] / risk_per_trade).mean() if len(wins) > 0 else 0,
        "avg_loss_r": (losses["net_pnl"] / risk_per_trade).mean() if len(losses) > 0 else 0,
        # Streaks
        "max_consecutive_wins": _max_consecutive(trades_df["is_win"], True),
        "max_consecutive_losses": _max_consecutive(trades_df["is_win"], False),
        # Daily stats
        "avg_daily_pnl": daily_pnl.mean(),
        "worst_day": daily_pnl.min(),
        "best_day": daily_pnl.max(),
        "days_traded": len(daily_pnl),
        "days_with_loss": (daily_pnl < 0).sum(),
        "days_with_cap_hit": (daily_pnl <= -CAPITAL_PER_VARIANT * 0.003).sum(),
        # Drawdown
        "max_dd_duration_bars": int(dd_dur.max()),
        "recovery_factor": abs(metrics["total_return_pct"] / metrics["max_drawdown_pct"]) if metrics["max_drawdown_pct"] != 0 else 0,
        # Direction breakdown
        "long_trades": len(trades_df[trades_df["direction"] == "long"]),
        "short_trades": len(trades_df[trades_df["direction"] == "short"]),
        "long_pnl": trades_df[trades_df["direction"] == "long"]["net_pnl"].sum(),
        "short_pnl": trades_df[trades_df["direction"] == "short"]["net_pnl"].sum(),
    }
    return report


def _format_report(report: dict) -> str:
    lines = [
        f"\n{'='*70}",
        f"PERIOD: {report['label']}",
        f"{'='*70}",
        f"Date range:     {report['start_date']} to {report['end_date']}",
        f"Initial equity: ${report['initial_equity']:,.2f}",
        f"Final equity:   ${report['final_equity']:,.2f}",
        f"Total return:   {report['total_return_pct']:.2f}%",
        f"CAGR:           {report['cagr_pct']:.2f}%",
        f"Ann. vol:       {report['ann_vol_pct']:.2f}%",
        f"Sharpe:         {report['sharpe']:.2f}",
        f"Max drawdown:   {report['max_drawdown_pct']:.2f}%",
        f"Recovery factor:{report['recovery_factor']:.2f}",
        f"\nTrades:         {report['num_trades']}",
        f"Win rate:       {report['win_rate_pct']:.2f}%",
        f"Loss rate:      {report['loss_rate_pct']:.2f}%",
        f"Profit factor:  {report['profit_factor']:.2f}",
        f"Payoff ratio:   {report['payoff_ratio']:.2f}",
        f"Avg trade:      ${report['avg_trade_pnl']:.2f}",
        f"Avg R:          {report['avg_r']:.2f}",
        f"\nWins:           {report['num_wins']}",
        f"Avg win:        ${report['avg_win']:.2f}",
        f"Max win:        ${report['max_win']:.2f}",
        f"Avg win (R):    {report['avg_win_r']:.2f}",
        f"\nLosses:         {report['num_losses']}",
        f"Avg loss:       ${report['avg_loss']:.2f}",
        f"Max loss:       ${report['max_loss']:.2f}",
        f"Avg loss (R):   {report['avg_loss_r']:.2f}",
        f"\nBest R:R trade: {report['best_r']:.2f}",
        f"Worst R:R trade:{report['worst_r']:.2f}",
        f"\nGross PnL:      ${report['gross_pnl']:,.2f}",
        f"Total costs:    ${report['total_cost']:,.2f}",
        f"Net PnL:        ${report['net_pnl']:,.2f}",
        f"\nMax consecutive wins:   {report['max_consecutive_wins']}",
        f"Max consecutive losses: {report['max_consecutive_losses']}",
        f"\nDaily stats:",
        f"  Days traded:          {report['days_traded']}",
        f"  Avg daily PnL:        ${report['avg_daily_pnl']:.2f}",
        f"  Best day:             ${report['best_day']:.2f}",
        f"  Worst day:            ${report['worst_day']:.2f}",
        f"  Days with loss:       {report['days_with_loss']}",
        f"  Days hitting cap:     {report['days_with_cap_hit']}",
        f"\nDirection breakdown:",
        f"  Long trades: {report['long_trades']} | PnL: ${report['long_pnl']:,.2f}",
        f"  Short trades: {report['short_trades']} | PnL: ${report['short_pnl']:,.2f}",
    ]
    return "\n".join(lines)


def main():
    df5_full = _load_parquet("BTCUSDT", 15)
    df1h_full = _load_parquet("BTCUSDT", 60)

    # Split periods
    is_mask = df5_full["timestamp"] < "2024-01-01"
    df5_is = df5_full[is_mask].copy().reset_index(drop=True)
    df5_oos = df5_full[~is_mask].copy().reset_index(drop=True)
    df1h_is = df1h_full[df1h_full["timestamp"] < "2024-01-01"].copy().reset_index(drop=True)
    df1h_oos = df1h_full[df1h_full["timestamp"] >= "2024-01-01"].copy().reset_index(drop=True)

    reports = []
    for label, df5, df1h in [
        ("IN-SAMPLE (2020-2023)", df5_is, df1h_is),
        ("OUT-OF-SAMPLE (2024-2026)", df5_oos, df1h_oos),
        ("TOTAL (2020-2026)", df5_full, df1h_full),
    ]:
        report = _run_period(df5, df1h, label)
        reports.append(report)
        print(_format_report(report))

    # Save JSON
    out_path = config.PROJECT_ROOT / "data" / "fvg_detailed_kpi_report.json"
    with open(out_path, "w") as f:
        json.dump(reports, f, indent=2, default=str)
    print(f"\nSaved detailed report to {out_path}")


if __name__ == "__main__":
    main()
