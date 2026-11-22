"""Interactive FVG momentum strategy backtest across timeframes.

The script asks for every parameter, runs the backtest with realistic FVG
midpoint limit fills, prints the results, then asks whether to try another
parameter set. It keeps looping until the user says no.

Default start date is 5 years ago from today.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.fvg.backtest import compute_metrics, run_backtest
from src.fvg.strategy import Direction
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


def ask(question: str, default: str) -> str:
    val = input(f"{question} [{default}]: ").strip()
    return val if val else default


def ask_float(question: str, default: float) -> float:
    while True:
        val = input(f"{question} [{default}]: ").strip()
        if not val:
            return default
        try:
            return float(val)
        except ValueError:
            print("  Please enter a number.")


def ask_int(question: str, default: int) -> int:
    while True:
        val = input(f"{question} [{default}]: ").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            print("  Please enter an integer.")


def ask_choice(question: str, options: list[str], default: str) -> str | None:
    opts_str = "/".join(options)
    while True:
        val = input(f"{question} [{opts_str}] [{default}]: ").strip()
        if not val:
            return default if default.lower() != "none" else None
        if val.lower() == "none":
            return None
        if val in options:
            return val
        print(f"  Please enter one of: {opts_str} or none")


def timeframe_defaults(tf: str) -> dict:
    """Return sensible default parameters scaled from the paper's 5m settings."""
    minutes = TIMEFRAME_CONFIG[tf]["minutes"]
    fvg_lookforward = max(1, round((2 * 60) / minutes))
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
    }


def gather_parameters(defaults: dict) -> dict:
    """Ask the user for every strategy parameter."""
    print("\n--- Strategy parameters (press Enter to keep default) ---")
    params = {}
    params["fvg_lookforward"] = ask_int("FVG lookforward bars", defaults["fvg_lookforward"])
    params["stop_pct"] = ask_float("Stop loss % (decimal, e.g. 0.004 = 0.4%)", defaults["stop_pct"])
    params["tp1_pct"] = ask_float("TP1 % (trail 1:1)", defaults["tp1_pct"])
    params["tp2_a_pct"] = ask_float("TP2 % for Grade A", defaults["tp2_a_pct"])
    params["tp2_a_plus_pct"] = ask_float("TP2 % for Grade A+", defaults["tp2_a_plus_pct"])
    params["runner_a_pct"] = ask_float("Runner max % for Grade A", defaults["runner_a_pct"])
    params["runner_a_plus_pct"] = ask_float("Runner max % for Grade A+", defaults["runner_a_plus_pct"])
    params["htf_proximity_pct"] = ask_float("HTF proximity %", defaults["htf_proximity_pct"])
    params["a_body_threshold"] = ask_float("Grade A body ratio threshold", defaults["a_body_threshold"])
    params["a_plus_body_threshold"] = ask_float("Grade A+ body ratio threshold", defaults["a_plus_body_threshold"])
    return params


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
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)
    return df


def run_one_backtest(tf: str, start_date: str) -> None:
    tf_cfg = TIMEFRAME_CONFIG[tf]
    defaults = timeframe_defaults(tf)

    print("\n============================================")
    print(f"  FVG backtest: {tf} entry / {tf_cfg['htf_interval']} HTF")
    print(f"  Data range: {start_date} -> today (~5 years)")
    print("============================================")

    params = gather_parameters(defaults)

    session = ask_choice("Session filter", ["Asia", "London", "NewYork", "Overlap"], "none")
    direction = ask_choice("Direction filter", ["long", "short"], "none")

    capital = ask_float("Starting capital", CAPITAL)
    risk_pct = ask_float("Risk per trade % (decimal, e.g. 0.005 = 0.5%)", RISK_PER_TRADE_PCT)
    entry_fee = ask_float("Entry fee % (decimal, e.g. 0.00055)", 0.00055)
    exit_fee = ask_float("Exit fee % (decimal, e.g. 0.00055)", 0.00055)

    common = {
        "a_body_threshold": params.pop("a_body_threshold"),
        "a_plus_body_threshold": params.pop("a_plus_body_threshold"),
        "direction_filter": Direction.LONG if direction == "long" else (Direction.SHORT if direction == "short" else None),
    }
    if session is not None:
        common["session_filter"] = session

    print("\nRunning with parameters:")
    print(json.dumps({**common, **params, "capital": capital, "risk_pct": risk_pct, "entry_fee": entry_fee, "exit_fee": exit_fee}, indent=2, default=str))

    start_ms = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    print(f"\nFetching {tf} klines ...")
    df_entry = fetch_klines("BTCUSDT", tf_cfg["interval"], start_ms, end_ms)
    print(f"  -> {len(df_entry)} rows, {df_entry['timestamp'].iloc[0]} -> {df_entry['timestamp'].iloc[-1]}")

    print(f"Fetching {tf_cfg['htf_interval']} HTF klines ...")
    df_htf = fetch_klines("BTCUSDT", tf_cfg["htf_interval"], start_ms, end_ms)
    print(f"  -> {len(df_htf)} rows, {df_htf['timestamp'].iloc[0]} -> {df_htf['timestamp'].iloc[-1]}")

    fixed_risk_amount = capital * risk_pct if USE_FIXED_RISK else None
    fixed_daily_loss_cap = fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else capital * 0.003

    print("\nRunning backtest with realistic FVG midpoint limit fills ...")
    result = run_backtest(
        df_entry,
        df_htf,
        capital=capital,
        risk_per_trade_pct=None if USE_FIXED_RISK else risk_pct,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=entry_fee,
        exit_cost_pct=exit_fee,
        **common,
        **params,
    )

    trades_df = result["trades"]
    equity_df = result["equity"]
    metrics = compute_metrics(equity_df, trades_df, capital)

    print("\n=== RESULTS ===")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"{k:30s}: {v:,.4f}")
        else:
            print(f"{k:30s}: {v}")

    if not trades_df.empty:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        os.makedirs(RESULTS_DIR, exist_ok=True)
        out_path = RESULTS_DIR / f"fvg_{tf}_{timestamp}.csv"
        trades_df.to_csv(out_path, index=False)
        print(f"\nSaved trade log to {out_path}")
    else:
        print("\nNo trades generated.")


def main():
    print("\n=== Interactive FVG Backtest ===")
    print("Default data window is the last 5 years.")

    five_years_ago = (datetime.now(timezone.utc) - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    start_date = ask("Start date (YYYY-MM-DD)", five_years_ago)

    tf = ask_choice("Entry timeframe", ["30m", "1h", "daily"], "1h")
    if tf is None:
        tf = "1h"

    while True:
        run_one_backtest(tf, start_date)

        again = input("\nRun again with different parameters? (y/n) [y]: ").strip().lower()
        if again in ("n", "no"):
            print("Exiting. Goodbye!")
            break
        # Keep same timeframe and start date, change parameters


if __name__ == "__main__":
    main()
