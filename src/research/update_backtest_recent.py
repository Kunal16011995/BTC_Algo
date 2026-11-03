"""Incrementally update the FVG backtest JSON with recent closed-bar data."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared import store
from src.fvg.backtest import compute_metrics, run_backtest
from src.shared.bybit_client import BybitClient

BASE_DIR = config.PROJECT_ROOT
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "fvg_backtest_tight.json"

TRADING_CAPITAL = 92_453.04
RISK_PER_TRADE_PCT = 0.005
USE_FIXED_RISK = True
DAILY_LOSS_CAP_MULTIPLIER = 2.0

VARIANTS = {
    "tight": {
        "stop_pct": 0.004,
        "tp1_pct": 0.004,
        "runner_a_pct": 0.020,
        "runner_a_plus_pct": 0.040,
        "htf_proximity_pct": 0.003,
    },
}

COMMON_PARAMS = {
    "fvg_lookforward": 8,
    "a_body_threshold": 0.52,
    "a_plus_body_threshold": 0.70,
    "direction_filter": None,
    "tp2_a_pct": 0.015,
    "tp2_a_plus_pct": 0.025,
}


def _load_klines(client: BybitClient, symbol: str, interval: str,
                 start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    rows = client.get_all_klines(symbol, interval, start_ms=start_ms, end_ms=end_ms)
    if not rows:
        raise RuntimeError(f"No {interval} klines returned from Bybit")
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Drop the forming bar to match the corrected live bot.
    if len(df) > 1:
        df = df.iloc[:-1].copy().reset_index(drop=True)
    return df


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    store.init()

    if not DATA_FILE.exists():
        print(f"{DATA_FILE} not found. Run compute_fvg_backtest.py first.")
        return

    with open(DATA_FILE) as f:
        existing = json.load(f)

    existing_trades = existing.get("trades", [])
    if not existing_trades:
        print("No existing trades. Run compute_fvg_backtest.py first.")
        return

    # Keep trades whose exit is before Oct 15 2026.
    cutoff_ms = int(pd.Timestamp("2026-10-15 00:00", tz="UTC").timestamp() * 1000)
    kept_trades = [t for t in existing_trades if int(t.get("exit_ts", 0) or 0) < cutoff_ms]
    print(f"Keeping {len(kept_trades)} existing trades before 2026-10-15")

    # Fetch only enough history for HTF indicators to stabilize (Oct 1 onward)
    # and run the backtest from Oct 15 onward. This avoids the slow full-history
    # fetch and the Bybit service errors on older pages.
    start_ms = int(pd.Timestamp("2026-10-01 00:00", tz="UTC").timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    df5 = _load_klines(client, "BTCUSDT", "15", start_ms=start_ms, end_ms=end_ms)
    df1h = _load_klines(client, "BTCUSDT", "60", start_ms=start_ms, end_ms=end_ms)
    print(f"Loaded M15: {df5.shape}, up to {df5['timestamp'].iloc[-1]}")
    print(f"Loaded H1:  {df1h.shape}, up to {df1h['timestamp'].iloc[-1]}")

    capital = TRADING_CAPITAL
    fixed_risk_amount = capital * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None
    fixed_daily_loss_cap = (
        fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else capital * 0.003
    )

    variant = "tight"
    params = VARIANTS[variant]

    result = run_backtest(
        df5,
        df1h,
        capital=capital,
        risk_per_trade_pct=None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **params,
    )

    # Keep only trades that entered on or after Oct 15 2026.
    trades_df = result["trades"]
    trades_df = trades_df[trades_df["entry_time"] >= pd.Timestamp("2026-10-15", tz="UTC")]

    print(f"New trades from Oct 15 onward: {len(trades_df)}")

    # Build trade dicts matching existing JSON format
    new_trades = []
    for _, row in trades_df.iterrows():
        side = "Buy" if row["direction"] == "long" else "Sell"
        qty = row["size_usd"] / row["entry_price"] if row["entry_price"] else 0.0
        entry_ts_ms = int(row["entry_time"].timestamp() * 1000)
        exit_ts_ms = int(row["exit_time"].timestamp() * 1000)
        new_trades.append({
            "id": f"{variant}-{pd.Timestamp.now(tz='UTC').strftime('%Y%m%d%H%M%S')}-{len(new_trades)}",
            "variant": variant,
            "symbol": "BTCUSDT",
            "side": side,
            "qty": round(qty, 6),
            "entry_price": float(row["entry_price"]),
            "exit_price": float(row["exit_price"]),
            "pnl": float(row["net_pnl"]),
            "entry_ts": entry_ts_ms,
            "exit_ts": exit_ts_ms,
            "raw": {
                "grade": row["grade"],
                "gross_pnl": float(row["gross_pnl"]),
                "cost": float(row["cost"]),
                "result_r": float(row["result_r"]),
                "exit_reason": row["exit_reason"],
                "direction": row["direction"],
                "size_usd": float(row["size_usd"]),
            },
        })

    all_trades = kept_trades + new_trades

    # Rebuild equity curve
    equity_points = []
    equity = capital
    # Seed at first trade exit if any
    if all_trades:
        first_exit = min(int(t["exit_ts"]) for t in all_trades)
        equity_points.append([int(first_exit / 1000) - 1, capital])
    for t in sorted(all_trades, key=lambda x: int(x["exit_ts"])):
        equity += t["pnl"]
        equity_points.append([int(t["exit_ts"] / 1000), equity])

    kpis = store.compute_kpis(all_trades, equity_points, capital)
    kpis["total_account_equity"] = kpis["final_equity"]
    kpis["allocated_capital"] = capital
    kpis["return_on_total_equity"] = kpis["total_return"]

    # Recompute metrics using a minimal equity DataFrame
    equity_df = pd.DataFrame(equity_points, columns=["timestamp", "equity"])
    equity_df["timestamp"] = pd.to_datetime(equity_df["timestamp"], unit="s", utc=True)
    trades_df_full = pd.DataFrame([
        {
            "entry_time": pd.to_datetime(t["entry_ts"], unit="ms", utc=True),
            "exit_time": pd.to_datetime(t["exit_ts"], unit="ms", utc=True),
            "direction": "long" if t["side"] == "Buy" else "short",
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "net_pnl": t["pnl"],
            "size_usd": t["raw"]["size_usd"],
            "cost": t["raw"]["cost"],
            "result_r": t["raw"]["result_r"],
            "exit_reason": t["raw"]["exit_reason"],
        }
        for t in all_trades
    ])
    metrics = compute_metrics(equity_df, trades_df_full, capital)

    payload = {
        "variant": variant,
        "params": {**COMMON_PARAMS, **params},
        "capital": capital,
        "total_account_equity": capital,
        "risk_per_trade_pct": None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        "fixed_risk_amount": fixed_risk_amount if USE_FIXED_RISK else None,
        "daily_loss_cap_pct": None,
        "fixed_daily_loss_cap": fixed_daily_loss_cap,
        "daily_loss_cap_multiplier": DAILY_LOSS_CAP_MULTIPLIER,
        "kpis": kpis,
        "metrics": metrics,
        "equity": {"ts": [p[0] for p in equity_points], "values": [p[1] for p in equity_points]},
        "trades": all_trades,
    }

    with open(DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"Saved updated backtest: {len(all_trades)} trades, final equity ${kpis['final_equity']:.2f}")
    print(f"Last trade exit: {pd.to_datetime(all_trades[-1]['exit_ts'], unit='ms', utc=True)}")


if __name__ == "__main__":
    main()
