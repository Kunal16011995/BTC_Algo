"""Recompute PnL for all backtest trades using live-bot fees (0.01% entry, 0.06% exit)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import store

DATA_FILE = Path("data/fvg_backtest_tight.json")

ENTRY_FEE_PCT = 0.0001
EXIT_FEE_PCT = 0.0006


def main():
    store.init()

    with open(DATA_FILE) as f:
        data = json.load(f)

    capital = float(data.get("capital", 92_453.04))
    trades = data.get("trades", [])

    print(f"Recomputing {len(trades)} trades with entry_fee={ENTRY_FEE_PCT} exit_fee={EXIT_FEE_PCT}")

    for t in trades:
        raw = t.get("raw", {})
        size_usd = float(raw.get("size_usd", 0) or 0)
        entry = float(t.get("entry_price", 0) or 0)
        exit_ = float(t.get("exit_price", 0) or 0)
        side = t.get("side", "")

        # Gross PnL
        if side == "Buy":
            gross = size_usd * (exit_ - entry) / entry
        else:
            gross = size_usd * (entry - exit_) / entry

        # New cost and net PnL
        new_cost = size_usd * (ENTRY_FEE_PCT + EXIT_FEE_PCT)
        net_pnl = gross - new_cost

        t["pnl"] = net_pnl
        raw["gross_pnl"] = gross
        raw["cost"] = new_cost

        # Recompute result_r based on intended risk
        stop_price = entry * (1 - 0.004) if side == "Buy" else entry * (1 + 0.004)
        risk_amount = size_usd * abs(entry - stop_price) / entry
        raw["result_r"] = net_pnl / risk_amount if risk_amount > 0 else 0.0

    # Rebuild equity curve
    equity_points = []
    equity = capital
    if trades:
        first_exit = min(int(t["exit_ts"]) for t in trades)
        equity_points.append([int(first_exit / 1000) - 1, capital])
    for t in sorted(trades, key=lambda x: int(x["exit_ts"])):
        equity += t["pnl"]
        equity_points.append([int(t["exit_ts"] / 1000), equity])

    # Recompute KPIs
    kpis = store.compute_kpis(trades, equity_points, capital)
    kpis["total_account_equity"] = kpis["final_equity"]
    kpis["allocated_capital"] = capital
    kpis["return_on_total_equity"] = kpis["total_return"]

    data["kpis"] = kpis
    data["equity"] = {"ts": [p[0] for p in equity_points], "values": [p[1] for p in equity_points]}
    data["risk_per_trade_pct"] = None
    data["fixed_risk_amount"] = capital * 0.005
    data["entry_cost_pct"] = ENTRY_FEE_PCT
    data["exit_cost_pct"] = EXIT_FEE_PCT

    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"Updated backtest: {len(trades)} trades, final equity ${kpis['final_equity']:.2f}")
    print(f"Total return: {kpis['total_return']:.2f}%")
    print(f"Win rate: {kpis['win_rate']:.1f}%")
    print(f"Sharpe: {kpis['sharpe_ratio']:.2f}")
    print(f"Max DD: {kpis['max_drawdown']:.2f}%")


if __name__ == "__main__":
    main()
