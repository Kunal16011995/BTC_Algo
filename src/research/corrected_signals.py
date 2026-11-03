"""Show what signals the corrected FVG bot would have produced for Oct 22-30."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from src.shared import config
from src.shared import store
from src.shared.bybit_client import BybitClient
from src.fvg.strategy import Direction, generate_setups

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


def main():
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    # Start well before the last 10 trades so the 500-bar window is warm.
    start_ms = int(pd.Timestamp("2026-10-15 00:00", tz="UTC").timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    rows = client.get_all_klines(SYMBOL, "15", start_ms=start_ms, end_ms=end_ms)
    m15_full = _klines_to_df(rows)
    # Drop the forming bar to match the corrected bot
    if len(m15_full) > 1:
        m15_full = m15_full.iloc[:-1].copy().reset_index(drop=True)

    # Actual forward-test trades keyed by the closed bar that preceded the order
    store.init()
    trades = store.load_trades_variant("tight")
    bar_ms = 15 * 60 * 1000
    actual_by_setup_ts: dict[pd.Timestamp, dict] = {}
    for t in trades:
        raw = t.get("raw", {}) or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        entry_ms = int(raw.get("createdTime", 0) or t.get("exit_ts", 0) or 0)
        if not entry_ms:
            continue
        setup_ts = pd.to_datetime(((entry_ms // bar_ms) - 1) * bar_ms, unit="ms", utc=True)
        actual_by_setup_ts[setup_ts] = t

    print("=" * 140)
    print("Corrected-bot signals for 2026-10-22 onward (only completed M15 bars)")
    print("=" * 140)
    print(f"{'Closed bar (UTC)':<22} {'Signal':<8} {'Grade':<6} {'Entry':>10} {'Stop':>10} {'TP1':>10} {'Runner':>10} {'HTF':>6} | Actual trade")
    print("-" * 140)

    # Walk forward using a 500-bar window like the bot
    for i in range(500, len(m15_full) + 1):
        m15 = m15_full.iloc[i - 500 : i].copy().reset_index(drop=True)
        h1 = _resample_m15_to_h1(m15, h1_bars=100)
        setups = generate_setups(m15.copy(), h1.copy(), **PARAMS)
        latest_ts = m15["timestamp"].iloc[-1]

        # Only show bars where a setup exists OR an actual trade was placed
        actual = actual_by_setup_ts.get(latest_ts)
        bar_setups = [s for s in setups if s.timestamp == latest_ts]

        if not bar_setups and actual is None:
            continue

        if bar_setups:
            for s in bar_setups:
                act = ""
                if actual is not None:
                    act = f"{actual['side']} @ {actual['entry_price']:.2f} PnL={actual['pnl']:.2f}"
                print(
                    f"{latest_ts.strftime('%Y-%m-%d %H:%M'):<22} "
                    f"{s.direction.name:<8} {s.grade.value:<6} "
                    f"{s.entry_price:>10.2f} {s.stop_price:>10.2f} "
                    f"{s.tp1_price:>10.2f} {s.runner_max_price:>10.2f} "
                    f"{'Y' if s.htf_aligned else 'N':>6} | {act}"
                )
        elif actual is not None:
            print(
                f"{latest_ts.strftime('%Y-%m-%d %H:%M'):<22} "
                f"{'NO SETUP':<8} {'':<6} {'':>10} {'':>10} {'':>10} {'':>10} {'':>6} | "
                f"{actual['side']} @ {actual['entry_price']:.2f} PnL={actual['pnl']:.2f} *** NOT A SIGNAL ***"
            )


if __name__ == "__main__":
    main()
