"""Fetch full M15/H1 history for FVG backtest research and save to cache."""
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared.bybit_client import BybitClient

BASE_DIR = config.PROJECT_ROOT
DATA_DIR = BASE_DIR / "data"
CACHE_M15 = DATA_DIR / "fvg_backtest_m15.pkl"
CACHE_H1 = DATA_DIR / "fvg_backtest_h1.pkl"

START_MS = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def fetch(interval: str) -> pd.DataFrame:
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )
    rows = client.get_all_klines("BTCUSDT", interval, start_ms=START_MS)
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Drop the current forming bar to match live/backtest behavior
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)
    return df


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    print("Fetching M15 klines...")
    m15 = fetch("15")
    print(f"M15: {len(m15)} rows, {m15['timestamp'].iloc[0]} -> {m15['timestamp'].iloc[-1]}")
    m15.to_pickle(CACHE_M15)
    print(f"Saved {CACHE_M15}")

    print("Fetching H1 klines...")
    h1 = fetch("60")
    print(f"H1:  {len(h1)} rows, {h1['timestamp'].iloc[0]} -> {h1['timestamp'].iloc[-1]}")
    h1.to_pickle(CACHE_H1)
    print(f"Saved {CACHE_H1}")


if __name__ == "__main__":
    main()
