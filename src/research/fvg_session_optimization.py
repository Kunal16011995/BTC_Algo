"""
Per-session parameter optimization for SSRN-6725880 FVG on BTCUSDT.

Tests each major trading session (Asia, London, Overlap, New York) with a
focused grid over stop_pct, tp1_pct, runner targets and HTF proximity.

Output: data/fvg_session_optimization_results.json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_fvg")
from src.fvg.backtest import compute_metrics, run_backtest

HISTORY_DIR = Path(r"C:\Dhan Algo\Systematic Trading Rules\backtests\ssrn-6725880_btc_m15_max\data")

TRADING_CAPITAL = 92_453.04
FIXED_RISK_AMOUNT = TRADING_CAPITAL * 0.005
DAILY_LOSS_CAP = FIXED_RISK_AMOUNT * 2.0

COMMON_PARAMS = {
    "fvg_lookforward": 8,
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
    "direction_filter": None,
}

SESSIONS = ["Asia", "London", "Overlap", "New York"]

GRID = {
    "stop_pct": [0.003, 0.004, 0.005],
    "tp1_pct": [0.003, 0.004, 0.005],
    "runner_a_pct": [0.015, 0.020, 0.030],
    "runner_a_plus_pct": [0.030, 0.040],
    "htf_proximity_pct": [0.003],
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


def _run(df5, df1h, session: str, params: dict):
    result = run_backtest(
        df5,
        df1h,
        capital=TRADING_CAPITAL,
        risk_per_trade_pct=None,
        fixed_risk_amount=FIXED_RISK_AMOUNT,
        fixed_daily_loss_cap=DAILY_LOSS_CAP,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        session_filter=session,
        **COMMON_PARAMS,
        **params,
    )
    trades_df = result["trades"]
    if trades_df.empty:
        return None
    metrics = compute_metrics(result["equity"], trades_df, TRADING_CAPITAL)
    return {
        "session": session,
        "params": params,
        "trades": len(trades_df),
        **metrics,
    }


def main():
    df5 = _load_parquet("BTCUSDT", 15)
    df1h = _load_parquet("BTCUSDT", 60)
    print(f"Loaded M15: {len(df5)} bars, H1: {len(df1h)} bars")

    rows = []
    total = len(SESSIONS)
    for i, session in enumerate(SESSIONS, 1):
        print(f"\n=== Session {i}/{total}: {session} ===")
        count = 0
        t0 = time.time()
        for stop_pct in GRID["stop_pct"]:
            for tp1_pct in GRID["tp1_pct"]:
                for runner_a_pct in GRID["runner_a_pct"]:
                    for runner_a_plus_pct in GRID["runner_a_plus_pct"]:
                        for htf_proximity_pct in GRID["htf_proximity_pct"]:
                            params = {
                                "stop_pct": stop_pct,
                                "tp1_pct": tp1_pct,
                                "runner_a_pct": runner_a_pct,
                                "runner_a_plus_pct": runner_a_plus_pct,
                                "htf_proximity_pct": htf_proximity_pct,
                            }
                            r = _run(df5, df1h, session, params)
                            count += 1
                            if r:
                                rows.append(r)
                            if count % 20 == 0:
                                print(f"  {count} combos tested, {len(rows)} valid so far ...")
        elapsed = time.time() - t0
        print(f"  Done: {count} combos in {elapsed:.0f}s, {len([r for r in rows if r['session'] == session])} valid results")

    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "fvg_session_optimization_results.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nSaved {len(rows)} results to {out_path}")

    df = pd.DataFrame(rows)
    print("\n=== TOP 5 BY SESSION (Sharpe) ===")
    for session in SESSIONS:
        sub = df[df["session"] == session]
        if sub.empty:
            continue
        print(f"\n{session}")
        print(
            sub.sort_values("sharpe", ascending=False)
            .head(5)
            [["params", "trades", "final_equity", "total_return_pct", "sharpe", "max_drawdown_pct", "win_rate_pct", "profit_factor"]]
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
