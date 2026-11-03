"""Diagnose whether the FVG bot is using the forming bar or the just-closed bar."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd

from src.shared import config
from src.shared import store
from src.shared.bybit_client import BybitClient
from src.fvg.strategy import Direction, generate_setups

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


def find_match(setups, side: str, entry_price: float, target_ts: pd.Timestamp, tol=0.003, time_tol_sec=7.5 * 60):
    for s in setups:
        if s.direction == Direction.LONG and side != "Buy":
            continue
        if s.direction == Direction.SHORT and side != "Sell":
            continue
        if abs((s.timestamp - target_ts).total_seconds()) > time_tol_sec:
            continue
        if entry_price > 0 and abs(s.entry_price - entry_price) / entry_price > tol:
            continue
        return s
    return None


def evaluate_window(client, entry_ms: int, side: str, entry_price: float, mode: str):
    """mode: 'forming' uses bar that contains entry_ms; 'closed' uses bar that closed just before entry_ms."""
    bar_ms = 15 * 60 * 1000
    entry_ts = pd.to_datetime(entry_ms, unit="ms", utc=True)

    if mode == "forming":
        # Bot's current behavior: latest bar returned by Bybit is the forming bar.
        target_open_ms = (entry_ms // bar_ms) * bar_ms
        target_ts = pd.to_datetime(target_open_ms, unit="ms", utc=True)
        end_ms = entry_ms
        start_ms = target_open_ms - 499 * bar_ms
    elif mode == "closed":
        # Correct behavior: use the bar that just closed before entry_ms.
        target_open_ms = ((entry_ms // bar_ms) - 1) * bar_ms
        target_ts = pd.to_datetime(target_open_ms, unit="ms", utc=True)
        end_ms = target_open_ms + bar_ms - 1
        start_ms = target_open_ms - 499 * bar_ms
    else:
        raise ValueError(mode)

    rows = client.get_all_klines(SYMBOL, "15", start_ms=start_ms, end_ms=end_ms)
    m15 = _klines_to_df(rows)
    h1 = _resample_m15_to_h1(m15, h1_bars=100)
    setups = generate_setups(m15.copy(), h1.copy(), **PARAMS)
    match = find_match(setups, side, entry_price, target_ts)
    return {
        "mode": mode,
        "target_ts": target_ts,
        "m15_bars": len(m15),
        "h1_bars": len(h1),
        "setups": len(setups),
        "match": match,
        "latest_bar_ts": m15["timestamp"].iloc[-1] if len(m15) else None,
    }


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

    print("=" * 120)
    print("Bar-timing diagnosis: 'forming' = bar that contains order time (bot's apparent behavior)")
    print("                      'closed'  = bar that closed just before order time (correct behavior)")
    print("=" * 120)

    for t in trades[-10:]:
        entry_ms = _entry_time_ms(t)
        side = t.get("side", "")
        entry_price = float(t.get("entry_price", 0) or 0)
        aligned = cached.get(t["id"], "unknown")

        forming = evaluate_window(client, entry_ms, side, entry_price, "forming")
        closed = evaluate_window(client, entry_ms, side, entry_price, "closed")

        print(f"\nTrade {t['id'][:12]} | {side} @ {entry_price:.2f} | aligned={aligned}")
        print(f"  order_time_utc = {pd.to_datetime(entry_ms, unit='ms', utc=True)}")
        print(f"  FORMING window: target={forming['target_ts']} latest_bar={forming['latest_bar_ts']} M15={forming['m15_bars']} H1={forming['h1_bars']} setups={forming['setups']} match={'YES' if forming['match'] else 'NO'}")
        if forming["match"]:
            s = forming["match"]
            print(f"    -> {s.direction.name} {s.grade.value} entry={s.entry_price:.2f} ts={s.timestamp} htf={s.htf_aligned}")
        print(f"  CLOSED  window: target={closed['target_ts']} latest_bar={closed['latest_bar_ts']} M15={closed['m15_bars']} H1={closed['h1_bars']} setups={closed['setups']} match={'YES' if closed['match'] else 'NO'}")
        if closed["match"]:
            s = closed["match"]
            print(f"    -> {s.direction.name} {s.grade.value} entry={s.entry_price:.2f} ts={s.timestamp} htf={s.htf_aligned}")

        if forming["match"] and not closed["match"]:
            print("  *** TIMING BUG: setup only appears on the forming bar, not on the just-closed bar ***")


if __name__ == "__main__":
    main()
