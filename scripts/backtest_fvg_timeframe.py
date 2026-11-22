"""Backtest the FVG momentum strategy on different entry timeframes.

Run examples:
    python -m scripts.backtest_fvg_timeframe --timeframe 30m
    python -m scripts.backtest_fvg_timeframe --timeframe 1h
    python -m scripts.backtest_fvg_timeframe --timeframe daily

Defaults try to keep the spirit of the SSRN paper (5m entry / 1h HTF / 2h FVG
window / ~0.09% stop) while scaling sensibly to larger timeframes. All
parameters can be overridden via CLI flags.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.fvg.backtest import compute_metrics, run_backtest
from src.shared import config
from src.shared.bybit_client import BybitClient

BASE_DIR = config.PROJECT_ROOT
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "research_results"

CAPITAL = 92_453.04
RISK_PER_TRADE_PCT = 0.005
USE_FIXED_RISK = True
DAILY_LOSS_CAP_MULTIPLIER = 2.0

# Map timeframe name -> (Bybit interval string, minutes per bar, default HTF interval)
TIMEFRAME_CONFIG = {
    "30m": {"interval": "30", "minutes": 30, "htf_interval": "60"},
    "1h": {"interval": "60", "minutes": 60, "htf_interval": "240"},
    "daily": {"interval": "D", "minutes": 24 * 60, "htf_interval": "W"},
}


def timeframe_defaults(tf: str) -> dict:
    """Return sensible default parameters scaled from the paper's 5m settings."""
    minutes = TIMEFRAME_CONFIG[tf]["minutes"]
    # Paper: 24 x 5m bars = 2 hours. Keep a ~2-hour FVG window.
    fvg_lookforward = max(1, round((2 * 60) / minutes))
    # Paper: ~0.09% stop on 5m. Scale roughly with sqrt(time) so higher TFs are wider.
    import math
    scale = math.sqrt(minutes / 5)
    stop_pct = 0.0009 * scale
    tp1_pct = stop_pct
    tp2_a_pct = 0.0040 * scale
    tp2_a_plus_pct = 0.0070 * scale
    runner_a_pct = 0.0050 * scale
    runner_a_plus_pct = 0.0100 * scale
    htf_proximity_pct = 0.0015 * scale
    return {
        "fvg_lookforward": fvg_lookforward,
        "stop_pct": stop_pct,
        "tp1_pct": tp1_pct,
        "tp2_a_pct": tp2_a_pct,
        "tp2_a_plus_pct": tp2_a_plus_pct,
        "runner_a_pct": runner_a_pct,
        "runner_a_plus_pct": runner_a_plus_pct,
        "htf_proximity_pct": htf_proximity_pct,
        "a_body_threshold": 0.52,
        "a_plus_body_threshold": 0.70,
        "direction_filter": None,
    }


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )
    rows = client.get_all_klines(symbol, interval, start_ms=start_ms, end_ms=end_ms)
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Drop the current forming bar to match live/backtest closed-bar logic.
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)
    return df


def main():
    parser = argparse.ArgumentParser(description="FVG strategy backtest across timeframes")
    parser.add_argument("--timeframe", required=True, choices=list(TIMEFRAME_CONFIG.keys()))
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2020-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=CAPITAL)
    parser.add_argument("--risk-pct", type=float, default=RISK_PER_TRADE_PCT)
    parser.add_argument("--entry-fee", type=float, default=0.00055)
    parser.add_argument("--exit-fee", type=float, default=0.00055)

    # Strategy parameter overrides
    parser.add_argument("--fvg-lookforward", type=int, default=None)
    parser.add_argument("--stop-pct", type=float, default=None)
    parser.add_argument("--tp1-pct", type=float, default=None)
    parser.add_argument("--tp2-a-pct", type=float, default=None)
    parser.add_argument("--tp2-a-plus-pct", type=float, default=None)
    parser.add_argument("--runner-a-pct", type=float, default=None)
    parser.add_argument("--runner-a-plus-pct", type=float, default=None)
    parser.add_argument("--htf-proximity-pct", type=float, default=None)
    parser.add_argument("--a-body-threshold", type=float, default=None)
    parser.add_argument("--a-plus-body-threshold", type=float, default=None)
    parser.add_argument("--session", default=None, choices=[None, "Asia", "London", "NewYork", "Overlap"])
    parser.add_argument("--direction", default=None, choices=[None, "long", "short"])

    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    tf_cfg = TIMEFRAME_CONFIG[args.timeframe]
    params = timeframe_defaults(args.timeframe)

    # Apply CLI overrides
    if args.fvg_lookforward is not None:
        params["fvg_lookforward"] = args.fvg_lookforward
    if args.stop_pct is not None:
        params["stop_pct"] = args.stop_pct
    if args.tp1_pct is not None:
        params["tp1_pct"] = args.tp1_pct
    if args.tp2_a_pct is not None:
        params["tp2_a_pct"] = args.tp2_a_pct
    if args.tp2_a_plus_pct is not None:
        params["tp2_a_plus_pct"] = args.tp2_a_plus_pct
    if args.runner_a_pct is not None:
        params["runner_a_pct"] = args.runner_a_pct
    if args.runner_a_plus_pct is not None:
        params["runner_a_plus_pct"] = args.runner_a_plus_pct
    if args.htf_proximity_pct is not None:
        params["htf_proximity_pct"] = args.htf_proximity_pct
    if args.a_body_threshold is not None:
        params["a_body_threshold"] = args.a_body_threshold
    if args.a_plus_body_threshold is not None:
        params["a_plus_body_threshold"] = args.a_plus_body_threshold
    if args.session is not None:
        params["session_filter"] = args.session
    if args.direction is not None:
        from src.fvg.strategy import Direction
        params["direction_filter"] = Direction.LONG if args.direction == "long" else Direction.SHORT

    common = {
        "a_body_threshold": params.pop("a_body_threshold"),
        "a_plus_body_threshold": params.pop("a_plus_body_threshold"),
        "direction_filter": params.pop("direction_filter", None),
    }
    if "session_filter" in params:
        common["session_filter"] = params.pop("session_filter")

    print(f"\n=== FVG backtest on {args.timeframe} entry / {tf_cfg['htf_interval']} HTF ===")
    print(f"Parameters: {json.dumps({**common, **params}, indent=2, default=str)}")

    start_ms = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    print(f"\nFetching {args.timeframe} klines from {args.start} ...")
    df_entry = fetch_klines(args.symbol, tf_cfg["interval"], start_ms, end_ms)
    print(f"  -> {len(df_entry)} rows, {df_entry['timestamp'].iloc[0]} -> {df_entry['timestamp'].iloc[-1]}")

    print(f"Fetching {tf_cfg['htf_interval']} HTF klines ...")
    df_htf = fetch_klines(args.symbol, tf_cfg["htf_interval"], start_ms, end_ms)
    print(f"  -> {len(df_htf)} rows, {df_htf['timestamp'].iloc[0]} -> {df_htf['timestamp'].iloc[-1]}")

    fixed_risk_amount = args.capital * args.risk_pct if USE_FIXED_RISK else None
    fixed_daily_loss_cap = fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else args.capital * 0.003

    print("\nRunning backtest with realistic FVG midpoint limit fills ...")
    result = run_backtest(
        df_entry,
        df_htf,
        capital=args.capital,
        risk_per_trade_pct=None if USE_FIXED_RISK else args.risk_pct,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=args.entry_fee,
        exit_cost_pct=args.exit_fee,
        **common,
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

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"fvg_{args.timeframe}_{timestamp}.csv"
    if not trades_df.empty:
        trades_df.to_csv(out_path, index=False)
        print(f"\nSaved trade log to {out_path}")
    else:
        print("\nNo trades generated.")


if __name__ == "__main__":
    main()
