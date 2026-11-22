"""Download BTCUSDT 5-minute OHLC data for the last 3 years and save to CSV."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared.bybit_client import BybitClient

BASE_DIR = config.PROJECT_ROOT
DATA_DIR = BASE_DIR / "data"

SYMBOL = "BTCUSDT"
INTERVAL = "5"  # 5-minute


def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=3 * 365)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    print(f"Downloading {SYMBOL} {INTERVAL}-minute OHLC from {start_dt.date()} to {end_dt.date()} ...")

    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )
    rows = client.get_all_klines(SYMBOL, INTERVAL, start_ms=start_ms, end_ms=end_ms)

    df = pd.DataFrame(
        rows,
        columns=["timestamp_ms", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp_ms"] = df["timestamp_ms"].astype(int)
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)

    df = df.sort_values("timestamp").reset_index(drop=True)
    # Drop the currently forming bar
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)

    out_path = DATA_DIR / f"{SYMBOL}_{INTERVAL}m_{start_dt.date()}_to_{end_dt.date()}.csv"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df[["timestamp", "open", "high", "low", "close", "volume", "turnover"]].to_csv(out_path, index=False)

    print(f"Downloaded {len(df)} rows")
    print(f"Range: {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
