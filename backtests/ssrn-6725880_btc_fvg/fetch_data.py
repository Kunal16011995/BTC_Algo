import os
import time
import datetime
from pathlib import Path

import requests
import pandas as pd

BASE_URL = os.getenv("BYBIT_PUBLIC_BASE_URL", "https://api.bybit.com")
SYMBOL = "BTCUSDT"
CATEGORY = "linear"
DATA_DIR = Path(__file__).parent / "data"


def fetch_klines(interval: str, start_ms: int, end_ms: int | None = None) -> list[list]:
    """Fetch klines from Bybit v5 API with backward pagination.

    Bybit returns the *newest* `limit` candles within [start, end].
    To walk back in time we shift `end` to just before the oldest candle
    returned in the previous batch.
    """
    url = f"{BASE_URL}/v5/market/kline"
    all_klines: list[list] = []
    current_end = end_ms
    max_retries = 5

    while True:
        params = {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "interval": interval,
            "start": start_ms,
            "limit": 1000,
        }
        if current_end is not None:
            params["end"] = current_end

        for attempt in range(max_retries):
            try:
                r = requests.get(url, params=params, timeout=15)
                r.raise_for_status()
                data = r.json()
                if data.get("retCode") != 0:
                    raise RuntimeError(f"Bybit error: {data}")
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                print(f"  retry {attempt + 1}/{max_retries}: {e}")
                time.sleep(2**attempt)

        klines = data["result"]["list"]
        if not klines:
            break

        all_klines.extend(klines)
        oldest_ts = int(klines[-1][0])
        newest_ts = int(klines[0][0])
        print(
            f"  {interval}: fetched {len(klines)} candles "
            f"({ts_to_str(oldest_ts)} -> {ts_to_str(newest_ts)}), total {len(all_klines)}"
        )

        if len(klines) < 1000:
            break
        if oldest_ts <= start_ms:
            break

        # Walk backward: request candles ending just before the current oldest
        current_end = oldest_ts - 1
        time.sleep(0.1)  # ~10 req/s, conservative

    return all_klines


def ts_to_str(ts_ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def klines_to_df(klines: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(
        klines,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def save_data(interval: str, start: datetime.datetime, end: datetime.datetime | None = None):
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) if end else None

    print(f"Fetching {interval} data from {ts_to_str(start_ms)} ...")
    klines = fetch_klines(interval, start_ms, end_ms)
    df = klines_to_df(klines)

    out_path = DATA_DIR / f"{SYMBOL}_{interval}.parquet"
    df.to_parquet(out_path, index=False)
    print(f"Saved {len(df)} rows to {out_path}")


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch max available history from Bybit BTCUSDT linear perpetual
    start_dt = datetime.datetime(2020, 3, 25, tzinfo=datetime.timezone.utc)
    end_dt = datetime.datetime.now(datetime.timezone.utc)

    save_data("5", start_dt, end_dt)
    save_data("60", start_dt, end_dt)
