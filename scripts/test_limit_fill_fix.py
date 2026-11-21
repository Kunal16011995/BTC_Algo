"""Quick test: verify the 21 Nov 03:15 setup does not fill in the corrected backtest."""
import os
from pathlib import Path

import pandas as pd

from src.fvg.backtest import simulate_trade
from src.fvg.strategy import generate_setups
from src.shared.bybit_client import BybitClient
from src.shared import config

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def fetch(interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )
    rows = client.get_all_klines("BTCUSDT", interval, start_ms=start_ms, end_ms=end_ms)
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    return df.sort_values("timestamp").reset_index(drop=True)


# Fetch a window around 21 Nov 2026
start_ms = int(pd.Timestamp("2026-11-15", tz="UTC").timestamp() * 1000)
end_ms = int(pd.Timestamp("2026-11-22", tz="UTC").timestamp() * 1000)

m15 = fetch("15", start_ms, end_ms)
h1 = fetch("60", start_ms, end_ms)

print(f"M15 range: {m15['timestamp'].iloc[0]} -> {m15['timestamp'].iloc[-1]}")

setups = generate_setups(
    m15,
    h1,
    fvg_lookforward=8,
    a_body_threshold=0.52,
    a_plus_body_threshold=0.70,
    direction_filter=None,
    tp2_a_pct=0.015,
    tp2_a_plus_pct=0.025,
    stop_pct=0.004,
    tp1_pct=0.004,
    runner_a_pct=0.020,
    runner_a_plus_pct=0.040,
    htf_proximity_pct=0.003,
)

print(f"Found {len(setups)} setups in window")

target = pd.Timestamp("2026-11-21 03:15:00", tz="UTC")
for setup in setups:
    if setup.timestamp == target:
        print(f"Found target setup at {setup.timestamp}: {setup.direction.name} entry={setup.entry_price:.2f} stop={setup.stop_price:.2f}")
        mask = m15["timestamp"] == setup.timestamp
        start_idx = int(mask.idxmax())
        trade = simulate_trade(
            setup, m15, start_idx, size_usd=100_000,
            entry_cost_pct=0.00055, exit_cost_pct=0.00055,
            fvg_lookforward=8,
        )
        if trade is None:
            print("  -> CORRECT: limit order expired unfilled")
        else:
            print(f"  -> BUG: trade filled at {trade.entry_price:.2f}, exited at {trade.exit_price:.2f}, pnl={trade.net_pnl:.2f}")
        break
else:
    print("No setup found at 2026-11-21 03:15:00")
