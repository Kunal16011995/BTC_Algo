"""
Generate A.3 Green/Red Weekly backtest JSON for the BTC dashboard.
Source: c:/Algos/NSE/WD GANN A.3 backtest outputs
Target: C:/Algos/Crypto/BTC/data/a3_backtest.json
"""

import json
import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, r"c:\Algos\NSE\WD GANN")
from backtest_A3_green_red_weekly import load_or_fetch_data, compute_green_red
from backtest_A3_sweep import run_backtest_variant
from src.shared import config
from src.shared import store

WD_GANN_DIR = Path(r"c:/Algos/NSE/WD GANN")
BTC_DIR = config.PROJECT_ROOT
DATA_DIR = BTC_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Best long-only variant from sweep
PARAMS = {
    "vol_target": 0.15,
    "vol_lookback": 36,
    "direction": "long_only",
    "confirmation": "hh_hl",
}
START_CAPITAL = 100_000.0


def drawdown_from_equity(ts_list, values):
    peak = None
    dd_ts, dd_vals = [], []
    for t, v in zip(ts_list, values):
        peak = v if peak is None else max(peak, v)
        dd_ts.append(t)
        dd_vals.append(round((v / peak - 1.0) * 100, 4))
    return {"ts": dd_ts, "values": dd_vals}


def static_drawdown_from_equity(ts_list, values, start_capital):
    dd_ts, dd_vals = [], []
    for t, v in zip(ts_list, values):
        dd_ts.append(t)
        dd_vals.append(round((v / start_capital - 1.0) * 100, 4))
    return {"ts": dd_ts, "values": dd_vals}


def main():
    df = load_or_fetch_data()
    df_bt, trades = run_backtest_variant(
        df,
        PARAMS["vol_target"],
        PARAMS["vol_lookback"],
        PARAMS["direction"],
        PARAMS["confirmation"],
        START_CAPITAL,
    )

    # Build equity points (ts in seconds)
    eq_df = df_bt[["timestamp", "capital"]].copy()
    eq_df["ts"] = eq_df["timestamp"].astype("int64") // 10**9
    eq_points = list(zip(eq_df["ts"].tolist(), eq_df["capital"].tolist()))

    ts_list = eq_df["ts"].tolist()
    values = eq_df["capital"].tolist()

    # Format trades for dashboard
    formatted_trades = []
    for t in trades:
        if "pnl" not in t:
            continue
        exit_ts = int(pd.Timestamp(t["exit_date"]).timestamp()) if pd.notna(t.get("exit_date")) else 0
        formatted_trades.append({
            "exit_ts": exit_ts * 1000,
            "variant": "a3_green_red_long",
            "side": "Buy" if t["type"] == "long" else "Sell",
            "qty": float(t.get("qty", 0) or 0) or float(abs(t.get("position", 0) or 0)),
            "entry_price": float(t["entry_price"]),
            "exit_price": float(t["exit_price"]),
            "pnl": float(t["pnl"]),
        })

    # Dashboard expects qty field; compute from position size if not present
    for i, t in enumerate(trades):
        if "pnl" not in t:
            continue
        # Find position size at entry bar
        entry_idx = int(t["entry_idx"])
        pos = float(abs(df_bt.iloc[entry_idx]["position"]))
        formatted_trades[i]["qty"] = pos

    kpis = store.compute_kpis(formatted_trades, eq_points, START_CAPITAL)
    kpis["total_account_equity"] = kpis["final_equity"]
    kpis["allocated_capital"] = START_CAPITAL
    kpis["return_on_total_equity"] = kpis["total_return"]

    output = {
        "capital": START_CAPITAL,
        "params": PARAMS,
        "kpis": kpis,
        "equity": {"ts": ts_list, "values": values},
        "drawdown": drawdown_from_equity(ts_list, values),
        "static_drawdown": static_drawdown_from_equity(ts_list, values, START_CAPITAL),
        "trades": formatted_trades,
    }

    out_path = DATA_DIR / "a3_backtest.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Saved {out_path}")
    print(f"Trades: {len(formatted_trades)}, Final equity: ${kpis['final_equity']:,.2f}, Sharpe: {kpis['sharpe_ratio']:.2f}")


if __name__ == "__main__":
    main()
