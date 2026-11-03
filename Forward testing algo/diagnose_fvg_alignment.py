"""Diagnose why the forward-test classifier misses live FVG setups."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd

import config
import store
from bybit_client import BybitClient
from strategy import Direction, generate_setups

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("diagnose")

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


def _klines_to_df(rows: list) -> pd.DataFrame:
    rows = sorted(rows, key=lambda r: int(r[0]))
    df = pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["open_time"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    return df[["timestamp", "open", "high", "low", "close", "volume", "turnover"]]


def _resample_m15_to_h1(m15: pd.DataFrame, h1_bars: int = 100) -> pd.DataFrame:
    df = m15.set_index("timestamp").copy()
    h1 = (
        df.resample("h")
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "turnover": "sum",
        })
        .dropna()
        .reset_index()
    )
    return h1.tail(h1_bars).reset_index(drop=True)


def _entry_time_ms(trade: dict) -> int:
    raw = trade.get("raw", {}) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    created = raw.get("createdTime")
    if created:
        return int(created)
    return int(trade.get("exit_ts", 0) or 0)


def find_matching_setup(setups, side: str, entry_price: float, entry_ts: pd.Timestamp, tol=0.003, time_tol_sec=7.5 * 60):
    for s in setups:
        if s.direction == Direction.LONG and side != "Buy":
            continue
        if s.direction == Direction.SHORT and side != "Sell":
            continue
        if abs((s.timestamp - entry_ts).total_seconds()) > time_tol_sec:
            continue
        if entry_price > 0 and abs(s.entry_price - entry_price) / entry_price > tol:
            continue
        return s
    return None


def main():
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )
    store.init()
    trades = store.load_trades_variant("tight")
    cached = json.loads(store.get_meta("fvg_aligned_trade_ids", "{}") or "{}")

    # Focus on the last 10 trades
    last10 = trades[-10:]

    print("=" * 100)
    print("Trade-by-trade diagnosis for the last 10 forward-test trades")
    print("=" * 100)

    for t in last10:
        entry_ms = _entry_time_ms(t)
        entry_ts = pd.to_datetime(entry_ms, unit="ms", utc=True)
        side = t.get("side", "")
        entry_price = float(t.get("entry_price", 0) or 0)
        aligned = cached.get(t["id"], "unknown")

        print(f"\nTrade {t['id'][:12]} | {side} @ {entry_price:.2f} | entry_ts={entry_ts} | aligned={aligned}")

        # The bot runs a few seconds after a 15-minute close, so the latest closed
        # bar at order time is the one that closed just before entry_ms.
        bar_ms = 15 * 60 * 1000
        latest_closed_open_ms = ((entry_ms // bar_ms) - 1) * bar_ms
        latest_closed_close_ms = latest_closed_open_ms + bar_ms - 1
        latest_closed_ts = pd.to_datetime(latest_closed_open_ms, unit="ms", utc=True)

        # Bot-style window: latest 500 M15 bars ending at the latest closed bar.
        bot_end_ms = latest_closed_close_ms
        bot_start_ms = latest_closed_open_ms - 499 * bar_ms
        bot_rows = client.get_all_klines(SYMBOL, "15", start_ms=bot_start_ms, end_ms=bot_end_ms)
        bot_m15 = _klines_to_df(bot_rows)
        bot_h1 = _resample_m15_to_h1(bot_m15, h1_bars=100)
        bot_setups = generate_setups(bot_m15.copy(), bot_h1.copy(), **PARAMS)
        bot_match = find_matching_setup(bot_setups, side, entry_price, latest_closed_ts)
        print(f"  BOT window  : M15={len(bot_m15)} H1={len(bot_h1)} setups={len(bot_setups)} match={'YES' if bot_match else 'NO'}")
        if bot_match:
            print(f"    -> matched setup: {bot_match.direction.name} {bot_match.grade.value} entry={bot_match.entry_price:.2f} ts={bot_match.timestamp} htf={bot_match.htf_aligned}")

        # Classifier-style window: +/- 3 days around the latest closed bar.
        cls_start_ms = latest_closed_open_ms - 3 * 24 * 60 * 60 * 1000
        cls_end_ms = latest_closed_close_ms + 3 * 24 * 60 * 60 * 1000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cls_end_ms = min(cls_end_ms, now_ms)
        cls_rows = client.get_all_klines(SYMBOL, "15", start_ms=cls_start_ms, end_ms=cls_end_ms)
        cls_m15 = _klines_to_df(cls_rows)
        cls_h1 = _resample_m15_to_h1(cls_m15, h1_bars=100)
        cls_setups = generate_setups(cls_m15.copy(), cls_h1.copy(), **PARAMS)
        cls_match = find_matching_setup(cls_setups, side, entry_price, latest_closed_ts)
        print(f"  CLASS window: M15={len(cls_m15)} H1={len(cls_h1)} setups={len(cls_setups)} match={'YES' if cls_match else 'NO'}")
        if cls_match:
            print(f"    -> matched setup: {cls_match.direction.name} {cls_match.grade.value} entry={cls_match.entry_price:.2f} ts={cls_match.timestamp} htf={cls_match.htf_aligned}")

        if bot_match and not cls_match:
            print("  *** Root cause candidate: setup reproducible with bot's 500-bar window but NOT with classifier's +/-3-day window ***")
        elif not bot_match and not cls_match:
            print("  *** Setup not reproducible even with bot window; investigate further ***")


if __name__ == "__main__":
    main()
