"""
Verify the last 10 FVG momentum forward-test trades against the strategy rules.
"""

import json
import time
import math
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

import store
from strategy import Direction, generate_setups

# Same params as classify.py
PARAMS = {
    "stop_pct": 0.004,
    "tp1_pct": 0.004,
    "runner_a_pct": 0.020,
    "runner_a_plus_pct": 0.040,
    "htf_proximity_pct": 0.003,
    "fvg_lookforward": 8,
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}

SYMBOL = "BTCUSDT"
INTERVAL = "15"


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 200) -> list:
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["start"] = start_ms
    if end_ms:
        params["end"] = end_ms
    for attempt in range(6):
        try:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
            if data.get("retCode") == 0:
                return data["result"]["list"]
        except Exception as e:
            print(f"Fetch error: {e}")
        time.sleep(min(5 * (2 ** attempt), 60))
    raise RuntimeError("Failed to fetch klines")


def fetch_all_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    all_rows = []
    cur_end = end_ms
    while cur_end > start_ms:
        rows = fetch_klines(symbol, interval, start_ms, cur_end, limit=200)
        if not rows:
            break
        all_rows.extend(rows)
        oldest_ts = int(rows[-1][0])
        if oldest_ts >= cur_end:
            break
        cur_end = oldest_ts - 1
        time.sleep(0.3)
    df = pd.DataFrame(all_rows, columns=["open_time", "open", "high", "low", "close", "volume", "turnover"])
    df["timestamp"] = pd.to_datetime(df["open_time"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    return df


def resample_m15_to_h1(m15: pd.DataFrame, h1_bars: int = 100) -> pd.DataFrame:
    df = m15.set_index("timestamp").copy()
    h1 = (
        df.resample("h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "turnover": "sum"})
        .dropna()
        .reset_index()
    )
    return h1.tail(h1_bars).reset_index(drop=True)


def verify_trade(t: dict):
    entry_ms = int(t.get("entry_ts", 0) or 0)
    if not entry_ms:
        raw = t.get("raw", {}) or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        entry_ms = int(raw.get("createdTime", 0) or 0)
    entry_ts = pd.to_datetime(entry_ms, unit="ms", utc=True)
    side = t.get("side", "")
    entry_price = float(t.get("entry_price", 0) or 0)
    exit_price = float(t.get("exit_price", 0) or 0)
    pnl = float(t.get("pnl", 0) or 0)

    # Fetch M15 + H1 around entry.  Match the corrected bot: drop the forming
    # bar and expect the setup to be on the bar that just closed before entry.
    start_ms = entry_ms - 3 * 24 * 60 * 60 * 1000
    end_ms = entry_ms + 3 * 24 * 60 * 60 * 1000
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = min(end_ms, now_ms)

    m15 = fetch_all_klines(SYMBOL, INTERVAL, start_ms, end_ms)
    if len(m15) > 1:
        m15 = m15.iloc[:-1].copy().reset_index(drop=True)
    h1 = resample_m15_to_h1(m15)

    setups = generate_setups(m15.copy(), h1.copy(), **PARAMS)

    bar_ms = 15 * 60 * 1000
    expected_setup_ms = ((entry_ms // bar_ms) - 1) * bar_ms
    expected_setup_ts = pd.to_datetime(expected_setup_ms, unit="ms", utc=True)

    # Find matching setup within 7.5 min of the expected closed bar and 0.3% price tolerance
    matched = None
    for s in setups:
        if s.direction == Direction.LONG and side != "Buy":
            continue
        if s.direction == Direction.SHORT and side != "Sell":
            continue
        time_diff = abs((s.timestamp - expected_setup_ts).total_seconds())
        if time_diff > 7.5 * 60:
            continue
        if entry_price > 0 and abs(s.entry_price - entry_price) / entry_price > 0.003:
            continue
        matched = s
        break

    return {
        "entry_ts": entry_ts.strftime("%Y-%m-%d %H:%M UTC"),
        "expected_setup_ts": expected_setup_ts.strftime("%Y-%m-%d %H:%M UTC"),
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl": pnl,
        "matched": matched is not None,
        "setup_direction": matched.direction.name if matched else None,
        "setup_grade": matched.grade.value if matched else None,
        "setup_entry": matched.entry_price if matched else None,
        "setup_htf_aligned": matched.htf_aligned if matched else None,
        "direction_aligned": (matched.direction == Direction.LONG and side == "Buy") or (matched.direction == Direction.SHORT and side == "Sell") if matched else None,
    }


def main():
    store.init()
    trades = store.load_trades_variant("tight")
    if not trades:
        print("No forward-test trades found.")
        return

    last10 = trades[-10:]
    print(f"Verifying last {len(last10)} forward-test trades...\n")

    results = []
    for i, t in enumerate(last10, 1):
        print(f"Trade {i}/{len(last10)}: {t.get('side', '')} @ {t.get('entry_price', 0)} ...")
        r = verify_trade(t)
        results.append(r)
        print(f"  Entry time: {r['entry_ts']}")
        print(f"  Expected setup bar: {r['expected_setup_ts']}")
        print(f"  PnL: ${r['pnl']:,.2f}")
        print(f"  Strategy-aligned (FVG setup found): {r['matched']}")
        print(f"  Setup direction: {r['setup_direction']}")
        print(f"  Trade direction aligned with setup: {r['direction_aligned']}")
        print(f"  Grade: {r['setup_grade']}, HTF aligned: {r['setup_htf_aligned']}")
        print()

    df = pd.DataFrame(results)
    print("\nSummary:")
    print(f"  Total checked: {len(df)}")
    print(f"  FVG-aligned: {df['matched'].sum()}")
    print(f"  Direction-aligned (among matched): {df['direction_aligned'].sum()}")
    print(f"  Losing trades: {(df['pnl'] < 0).sum()}")
    print(f"  Losing trades that were NOT FVG-aligned: {((df['pnl'] < 0) & (~df['matched'])).sum()}")


if __name__ == "__main__":
    main()
