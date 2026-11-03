"""Simulate PnL of the corrected-bot signals for Oct 15-30, 2026."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

import config
from bybit_client import BybitClient
from fvg_paper_bot import PaperTrade, _process_bar, RISK_PER_TRADE, ENTRY_FEE_PCT
from strategy import Direction, generate_setups

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


@dataclass
class SimTrade:
    setup: object
    size_usd: float
    qty: float
    entry_cost: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    trailing_stop: float | None = None
    extreme_price: float = 0.0

    def __post_init__(self):
        if self.extreme_price == 0.0:
            self.extreme_price = self.setup.entry_price


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


def simulate():
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    start_ms = int(pd.Timestamp("2026-10-15 00:00", tz="UTC").timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    rows = client.get_all_klines(SYMBOL, "15", start_ms=start_ms, end_ms=end_ms)
    m15_full = _klines_to_df(rows)
    if len(m15_full) > 1:
        m15_full = m15_full.iloc[:-1].copy().reset_index(drop=True)

    capital = 92_453.04  # matches the live bot's fixed trading capital
    trades: list[dict] = []
    open_trades: list[SimTrade] = []

    for i in range(500, len(m15_full) + 1):
        m15 = m15_full.iloc[i - 500 : i].copy().reset_index(drop=True)
        h1 = _resample_m15_to_h1(m15, h1_bars=100)
        setups = generate_setups(m15.copy(), h1.copy(), **PARAMS)
        latest_ts = m15["timestamp"].iloc[-1]
        latest_bar = m15.iloc[-1]

        # Process open trades
        still_open: list[SimTrade] = []
        for t in open_trades:
            closed = _process_bar(
                PaperTrade(
                    variant="tight",
                    setup=t.setup,
                    size_usd=t.size_usd,
                    qty=t.qty,
                    entry_cost=t.entry_cost,
                    tp1_hit=t.tp1_hit,
                    tp2_hit=t.tp2_hit,
                    trailing_stop=t.trailing_stop,
                    extreme_price=t.extreme_price,
                ),
                latest_bar,
            )
            if closed is None:
                still_open.append(t)
                continue
            capital += closed["pnl"]
            trades.append({
                "entry_ts": t.setup.timestamp,
                "exit_ts": latest_ts,
                "side": closed["side"],
                "entry_price": closed["entry_price"],
                "exit_price": closed["exit_price"],
                "pnl": closed["pnl"],
                "reason": closed["raw"]["exit_reason"],
                "capital": capital,
            })
        open_trades = still_open

        # Enter new setups that complete on this closed bar
        for s in setups:
            if s.timestamp != latest_ts:
                continue
            if any(t.setup.timestamp == s.timestamp and t.setup.direction == s.direction for t in open_trades):
                continue
            stop_distance = abs(s.entry_price - s.stop_price) / s.entry_price
            if stop_distance <= 0:
                continue
            size_usd = RISK_PER_TRADE / stop_distance
            qty = size_usd / s.entry_price
            entry_cost = size_usd * ENTRY_FEE_PCT
            open_trades.append(
                SimTrade(
                    setup=s,
                    size_usd=size_usd,
                    qty=qty,
                    entry_cost=entry_cost,
                )
            )

    # Force-close any remaining open trades at the last close
    for t in open_trades:
        entry = t.setup.entry_price
        exit_price = m15_full["close"].iloc[-1]
        direction = t.setup.direction
        if direction == Direction.LONG:
            gross_pnl = t.size_usd * (exit_price - entry) / entry
        else:
            gross_pnl = t.size_usd * (entry - exit_price) / entry
        exit_cost = t.size_usd * 0.0006
        net_pnl = gross_pnl - t.entry_cost - exit_cost
        capital += net_pnl
        trades.append({
            "entry_ts": t.setup.timestamp,
            "exit_ts": m15_full["timestamp"].iloc[-1],
            "side": "Buy" if direction == Direction.LONG else "Sell",
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl": net_pnl,
            "reason": "force_close",
            "capital": capital,
        })

    return trades, capital


def main():
    trades, final_capital = simulate()
    df = pd.DataFrame(trades)
    if df.empty:
        print("No corrected-bot trades in this period.")
        return

    df["win"] = df["pnl"] > 0
    wins = df[df["pnl"] > 0]["pnl"]
    losses = df[df["pnl"] <= 0]["pnl"]

    print("=" * 110)
    print("Corrected-bot simulated trades (Oct 15 - present, $92,453.04 bot capital)")
    print("=" * 110)
    print(f"{'Entry (UTC)':<18} {'Exit (UTC)':<18} {'Side':<6} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Reason':<15}")
    print("-" * 110)
    for _, r in df.iterrows():
        print(
            f"{r['entry_ts'].strftime('%Y-%m-%d %H:%M'):<18} "
            f"{r['exit_ts'].strftime('%Y-%m-%d %H:%M'):<18} "
            f"{r['side']:<6} {r['entry_price']:>10.2f} {r['exit_price']:>10.2f} "
            f"{r['pnl']:>10.2f} {r['reason']:<15}"
        )

    print("=" * 110)
    print(f"Total trades: {len(df)}")
    print(f"Win rate: {df['win'].mean()*100:.1f}%")
    print(f"Gross profit: ${wins.sum():,.2f}" if len(wins) else "Gross profit: $0.00")
    print(f"Gross loss: ${losses.sum():,.2f}" if len(losses) else "Gross loss: $0.00")
    print(f"Net PnL: ${df['pnl'].sum():,.2f}")
    print(f"Final capital: ${final_capital:,.2f}")
    print(f"Return: {(final_capital/92_453.04 - 1)*100:+.2f}%")


if __name__ == "__main__":
    main()
