"""Check when the FVG midpoint 58517.25 was first touched on 1-minute data."""
import requests
from datetime import datetime, timezone

PUBLIC_URL = 'https://api.bybit.com'
SYMBOL = 'BTCUSDT'
INTERVAL = '1'
LIMIT = 1000

# Window: 2026-11-21 02:30 to 05:30 UTC
start = int(datetime(2026, 11, 21, 2, 30, tzinfo=timezone.utc).timestamp() * 1000)
end = int(datetime(2026, 11, 21, 5, 30, tzinfo=timezone.utc).timestamp() * 1000)

limit_price = 58517.25

params = {
    'category': 'linear',
    'symbol': SYMBOL,
    'interval': INTERVAL,
    'limit': LIMIT,
    'start': str(start),
    'end': str(end),
}

resp = requests.get(f'{PUBLIC_URL}/v5/market/kline', params=params, headers={'User-Agent': 'BTC-momentum-bot/1.0'}, timeout=30)
resp.raise_for_status()
data = resp.json()
rows = data['result']['list']
# rows are newest-first
rows.reverse()

print(f"Fetched {len(rows)} 1-min candles")
print(f"First candle: {rows[0]}")
print(f"Last candle: {rows[-1]}")
print()

# Only count touches at/after the signal bar close time (03:30:00)
signal_dt = datetime(2026, 11, 21, 3, 30, 0, tzinfo=timezone.utc)
first_touch = None
print(f"Looking for first 1-min high >= {limit_price} at or after {signal_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC...")
print()
for row in rows:
    ts_ms = int(row[0])
    o = float(row[1])
    h = float(row[2])
    l = float(row[3])
    c = float(row[4])
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    marker = ''
    if h >= limit_price and first_touch is None and dt >= signal_dt:
        first_touch = (dt, o, h, l, c, row)
        marker = ' <-- FIRST POST-SIGNAL TOUCH'
    # print all candles around the window for context
    if datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc) <= dt <= datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc):
        print(f"{dt.strftime('%H:%M')}  O={o:>8.2f} H={h:>8.2f} L={l:>8.2f} C={c:>8.2f}{marker}")

if first_touch is None:
    print(f"\nPrice never touched {limit_price} at or after signal time.")
else:
    latency = (first_touch[0] - signal_dt).total_seconds()
    print(f"\nLatency from 03:30:00 signal close to first post-signal touch: {latency:.1f} seconds")
