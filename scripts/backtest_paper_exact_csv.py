"""Run the FVG strategy with the paper's exact parameters using a local 5m CSV.

Reads the downloaded 5-minute CSV, builds a 1-hour HTF dataframe from it,
and runs the backtest with realistic FVG midpoint limit fills.

Paper parameters:
- Entry TF: 5m, HTF: 1h
- Momentum leg: >=2 candles, body ratio >=0.70, aligned, swing breach
- FVG scan: next 24 bars (2 hours)
- HTF confluence: within 12 pips of EMA20/EMA50/POC30 on 1h
- Grade A+: body >=0.70 + HTF aligned
- Grade A:  body >=0.52
- SL / TP1: 9 pips
- TP2: 20 pips (A), 35 pips (A+)
- Runner: 25 pips (A), 50 pips (A+)
- Sessions: London/Overlap/New York only (Asia excluded)
- Risk per trade: fixed $250
- Daily loss cap: -$150

For BTC a "pip" is ambiguous; this script uses a configurable pip_value
(default $10) so 9 pips = $90 stop distance. Use --pip-value to change it.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.fvg.backtest import compute_metrics, run_backtest
from src.fvg.strategy import Direction

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "research_results"


def load_5m_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Drop the currently forming (last) bar to match closed-bar logic.
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)
    return df


def resample_to_1h(df5: pd.DataFrame) -> pd.DataFrame:
    df = df5.set_index("timestamp").resample("h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "turnover": "sum",
    }).dropna().reset_index()
    # Drop the last 1h bar if it is still forming (resample may produce partial bar)
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)
    return df


def find_csv() -> Path:
    candidates = sorted(DATA_DIR.glob("BTCUSDT_5m_*.csv"))
    if not candidates:
        raise FileNotFoundError(f"No BTCUSDT 5m CSV found in {DATA_DIR}")
    return candidates[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=None, help="Path to 5m CSV")
    parser.add_argument("--pip-value", type=float, default=10.0, help="Dollar value of one 'pip' for BTC")
    parser.add_argument("--capital", type=float, default=50_000.0)
    parser.add_argument("--risk", type=float, default=250.0, help="Fixed dollar risk per trade")
    parser.add_argument("--daily-cap", type=float, default=150.0, help="Daily loss cap in dollars")
    parser.add_argument("--entry-fee", type=float, default=0.0001)
    parser.add_argument("--exit-fee", type=float, default=0.0006)
    parser.add_argument("--session", default=None, choices=[None, "Asia", "London", "NewYork", "Overlap"])
    parser.add_argument("--direction", default=None, choices=[None, "long", "short"])
    args = parser.parse_args()

    csv_path = args.csv or find_csv()
    print(f"Loading 5m data from {csv_path} ...")
    df5 = load_5m_csv(csv_path)
    print(f"  -> {len(df5)} rows, {df5['timestamp'].iloc[0]} -> {df5['timestamp'].iloc[-1]}")

    print("Building 1h HTF data ...")
    df1h = resample_to_1h(df5)
    print(f"  -> {len(df1h)} rows, {df1h['timestamp'].iloc[0]} -> {df1h['timestamp'].iloc[-1]}")

    params = {
        # Paper exact pip-based parameters
        "stop_pips": 9.0,
        "tp1_pips": 9.0,
        "tp2_a_pips": 20.0,
        "tp2_a_plus_pips": 35.0,
        "runner_a_pips": 25.0,
        "runner_a_plus_pips": 50.0,
        "htf_proximity_pips": 12.0,
        "pip_value": args.pip_value,
        "use_pips": True,
        # Paper exact other parameters
        "fvg_lookforward": 24,
        "a_body_threshold": 0.52,
        "a_plus_body_threshold": 0.70,
        # Sessions: exclude Asia by setting session_filter to one of the allowed,
        # or leave None to include all allowed (London, Overlap, NY) because
        # strategy.py already excludes None sessions.
        "session_filter": args.session,
        "direction_filter": Direction.LONG if args.direction == "long" else (Direction.SHORT if args.direction == "short" else None),
    }

    print("\nRunning backtest with paper-exact parameters ...")
    print(f"  Capital: ${args.capital:,.2f}, Risk/trade: ${args.risk:,.2f}, Daily cap: ${args.daily_cap:,.2f}")
    print(f"  Pip value: ${args.pip_value:.2f} -> 9-pip stop = ${9 * args.pip_value:.2f}")

    result = run_backtest(
        df5,
        df1h,
        capital=args.capital,
        risk_per_trade_pct=None,
        fixed_risk_amount=args.risk,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=args.daily_cap,
        entry_cost_pct=args.entry_fee,
        exit_cost_pct=args.exit_fee,
        **params,
    )

    trades_df = result["trades"]
    equity_df = result["equity"]
    metrics = compute_metrics(equity_df, trades_df, args.capital)

    print("\n=== RESULTS ===")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"{k:30s}: {v:,.4f}")
        else:
            print(f"{k:30s}: {v}")

    if not trades_df.empty:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = RESULTS_DIR / f"fvg_paper_exact_{timestamp}.csv"
        trades_df.to_csv(out_path, index=False)
        print(f"\nSaved trade log to {out_path}")
    else:
        print("\nNo trades generated.")


if __name__ == "__main__":
    main()
