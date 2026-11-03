"""
Fetch maximum available daily BTCUSDT klines from Bybit public API
and save to a CSV file for backtesting.
"""

import os
import time
import pandas as pd
from datetime import datetime, timezone

import config
from bybit_client import BybitClient


def fetch_all_daily_klines(symbol='BTCUSDT', interval='D', sleep_seconds=0.5):
    """
    Paginate backwards through Bybit v5 klines until no more data.
    Returns a DataFrame sorted oldest-first.
    """
    client = BybitClient(
        api_key=config.BYBIT_API_KEY,
        api_secret=config.BYBIT_API_SECRET,
        demo_base_url=config.BYBIT_DEMO_BASE_URL,
        public_base_url=config.BYBIT_PUBLIC_BASE_URL,
    )

    all_rows = []
    limit = 1000
    # Start from now and go backwards
    end_ms = int(time.time() * 1000)

    print(f'Fetching {symbol} {interval} klines from Bybit...')

    while True:
        batch = client.get_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
            end_ms=end_ms,
        )

        if not batch:
            print('No more data returned.')
            break

        all_rows.extend(batch)
        oldest_ts = int(batch[-1][0])
        newest_ts = int(batch[0][0])
        oldest_dt = datetime.fromtimestamp(oldest_ts / 1000, tz=timezone.utc)
        newest_dt = datetime.fromtimestamp(newest_ts / 1000, tz=timezone.utc)
        print(f'Fetched {len(batch)} rows. Range: {oldest_dt} to {newest_dt}')

        # If we got fewer than limit rows, we've reached the beginning
        if len(batch) < limit:
            print('Reached earliest available data.')
            break

        # Move end_ms to just before the oldest candle in this batch
        end_ms = oldest_ts - 1
        time.sleep(sleep_seconds)

    # Build DataFrame
    df = pd.DataFrame(
        all_rows,
        columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'],
    )

    # Convert types
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms', utc=True)
    numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'turnover']
    for col in numeric_cols:
        df[col] = df[col].astype(float)

    # Remove duplicates and sort oldest-first
    df = df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)

    return df


def main():
    df = fetch_all_daily_klines(symbol='BTCUSDT', interval='D')
    output_path = os.path.join(os.path.dirname(__file__), 'btc_daily_bybit.csv')
    df.to_csv(output_path, index=False)
    print(f'\nSaved {len(df)} daily candles to {output_path}')
    print(f'Date range: {df["timestamp"].min()} to {df["timestamp"].max()}')
    print(df.head())
    print(df.tail())


if __name__ == '__main__':
    main()
