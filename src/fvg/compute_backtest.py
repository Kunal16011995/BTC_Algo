"""
Pre-compute SSRN-6725880 FVG backtest results for the dashboard Backtest tab.

Fetches BTCUSDT M15 and H1 klines from Bybit, runs the tight variant using the
same backtest engine as the GitHub repo, and writes:

    data/fvg_backtest_tight.json

Run with:
    python compute_fvg_backtest.py
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.shared import config
from src.shared import store
from src.fvg.backtest import compute_metrics, run_backtest
from src.shared.bybit_client import BybitClient

BASE_DIR = config.PROJECT_ROOT
DATA_DIR = BASE_DIR / "data"

CACHE_M15_PATH = DATA_DIR / "fvg_backtest_m15.pkl"
CACHE_H1_PATH = DATA_DIR / "fvg_backtest_h1.pkl"

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
    """Fetch klines from Bybit and return a sorted DataFrame."""
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
    return df


def _load_cached_klines(cache_path: Path) -> pd.DataFrame | None:
    """Load cached klines if available."""
    if not cache_path.exists():
        return None
    try:
        df = pd.read_pickle(cache_path)
        print(f"Loaded cache {cache_path.name}: {len(df)} rows up to {df['timestamp'].iloc[-1]}")
        return df
    except Exception as exc:
        print(f"Could not load cache {cache_path.name}: {exc}; re-fetching from scratch")
        return None


def _save_cached_klines(df: pd.DataFrame, cache_path: Path) -> None:
    """Save klines cache to disk."""
    df.to_pickle(cache_path)


def _load_or_update_klines(
    client: BybitClient,
    symbol: str,
    interval: str,
    cache_path: Path,
    default_start_ms: int,
) -> pd.DataFrame:
    """Load cached klines and incrementally fetch only new bars."""
    cached = _load_cached_klines(cache_path)
    now_ms = int(time.time() * 1000)

    if cached is not None and not cached.empty:
        # Fetch from the last cached timestamp onwards.
        last_ts_ms = int(cached["timestamp"].iloc[-1].timestamp() * 1000)
        start_ms = last_ts_ms + 1
        print(f"Fetching {interval} klines incremental from {cached['timestamp'].iloc[-1]} ...")
        new_df = _load_klines(client, symbol, interval, start_ms=start_ms, end_ms=now_ms)
        if new_df.empty:
            print(f"No new {interval} bars available")
            df = cached
        else:
            df = pd.concat([cached, new_df], ignore_index=True)
            df = df.drop_duplicates(subset=["timestamp"], keep="last")
            df = df.sort_values("timestamp").reset_index(drop=True)
            print(f"Appended {len(new_df)} new {interval} rows; total {len(df)}")
    else:
        print(f"Fetching {interval} klines from scratch ...")
        df = _load_klines(client, symbol, interval, start_ms=default_start_ms, end_ms=now_ms)

    _save_cached_klines(df, cache_path)
    return df


def _build_trade_dict(row: pd.Series, variant: str) -> dict:
    side = "Buy" if row["direction"] == "long" else "Sell"
    qty = row["size_usd"] / row["entry_price"] if row["entry_price"] else 0.0
    entry_ts_ms = int(row["entry_time"].timestamp() * 1000)
    exit_ts_ms = int(row["exit_time"].timestamp() * 1000)
    return {
        "id": f"{variant}-{uuid.uuid4().hex[:12]}",
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
    }


def _load_existing_backtest(variant: str) -> dict | None:
    """Load previously computed backtest JSON so we can extend it incrementally."""
    path = DATA_DIR / f"fvg_backtest_{variant}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
        trades = payload.get("trades", [])
        equity_ts = payload.get("equity", {}).get("ts", [])
        last_exit_ts = max(t["exit_ts"] for t in trades) if trades else None
        last_equity_ts = max(equity_ts) if equity_ts else None
        last_ts = max(filter(None, [last_exit_ts, last_equity_ts]), default=None)
        if last_ts is None:
            return None
        payload["_last_ts"] = last_ts
        print(f"Existing backtest has {len(trades)} trades up to ts={last_ts}")
        return payload
    except Exception as exc:
        print(f"Could not load existing backtest: {exc}; rebuilding from scratch")
        return None


def _run_variant(client: BybitClient, variant: str, params: dict, capital: float):
    print(f"Running backtest for variant: {variant} ...")
    t0 = time.time()

    existing = _load_existing_backtest(variant)

    # Lookback window for HTF warm-up when extending an existing backtest.
    lookback_days = int(os.getenv("BACKTEST_LOOKBACK_DAYS", "90"))
    lookback_ms = lookback_days * 24 * 60 * 60 * 1000

    if existing is not None:
        last_old_ts_ms = existing["_last_ts"]
        # Fetch enough history before the last old trade so HTF levels warm up.
        fetch_start_ms = max(0, last_old_ts_ms - lookback_ms)
        run_capital = float(existing["kpis"]["final_equity"])
        print(f"Extending existing backtest from ts={last_old_ts_ms} with capital ${run_capital:,.2f}")
    else:
        start_date_str = os.getenv("BACKTEST_START_DATE", "2025-01-01")
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        fetch_start_ms = int(start_dt.timestamp() * 1000)
        run_capital = capital
        print(f"Building backtest from {start_date_str} with capital ${run_capital:,.2f}")

    now_ms = int(time.time() * 1000)
    df5 = _load_klines(client, "BTCUSDT", "15", start_ms=fetch_start_ms, end_ms=now_ms)
    df1h = _load_klines(client, "BTCUSDT", "60", start_ms=fetch_start_ms, end_ms=now_ms)

    # Drop the current forming bar so the backtest uses only completed bars,
    # matching the corrected live bot and avoiding ephemeral end-of-window setups.
    if len(df5) > 1:
        df5 = df5.iloc[:-1].copy().reset_index(drop=True)
    if len(df1h) > 1:
        df1h = df1h.iloc[:-1].copy().reset_index(drop=True)

    print(f"Loaded M15: {df5.shape}, from {df5['timestamp'].iloc[0]} to {df5['timestamp'].iloc[-1]}")
    print(f"Loaded H1:  {df1h.shape}, from {df1h['timestamp'].iloc[0]} to {df1h['timestamp'].iloc[-1]}")

    fixed_risk_amount = run_capital * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None
    fixed_daily_loss_cap = (
        fixed_risk_amount * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else run_capital * 0.003
    )

    result = run_backtest(
        df5,
        df1h,
        capital=run_capital,
        risk_per_trade_pct=None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        fixed_risk_amount=fixed_risk_amount,
        daily_loss_cap_pct=None,
        fixed_daily_loss_cap=fixed_daily_loss_cap,
        entry_cost_pct=0.0001,
        exit_cost_pct=0.0006,
        **COMMON_PARAMS,
        **params,
    )

    new_equity_df = result["equity"]
    new_trades_df = result["trades"]

    new_equity_points = [
        [int(ts.timestamp()), float(eq)]
        for ts, eq in zip(new_equity_df["timestamp"], new_equity_df["equity"])
    ]
    new_trades = [_build_trade_dict(row, variant) for _, row in new_trades_df.iterrows()]

    if existing is not None:
        last_old_ts = existing["_last_ts"]
        old_trades = [t for t in existing["trades"] if t["exit_ts"] <= last_old_ts]
        old_equity_ts = existing["equity"]["ts"]
        old_equity_values = existing["equity"]["values"]
        old_equity_points = [
            [int(ts), float(eq)]
            for ts, eq in zip(old_equity_ts, old_equity_values)
            if int(ts) <= last_old_ts
        ]

        # Drop new data that overlaps the old window to avoid duplicates.
        combined_trades = old_trades + [t for t in new_trades if t["exit_ts"] > last_old_ts]
        combined_equity_points = (
            old_equity_points + [p for p in new_equity_points if p[0] > last_old_ts]
        )

        # Build combined DataFrames for metrics recomputation.
        combined_equity_df = pd.DataFrame(combined_equity_points, columns=["timestamp", "equity"])
        combined_equity_df["timestamp"] = pd.to_datetime(combined_equity_df["timestamp"], unit="s", utc=True)
        combined_trades_df = pd.concat(
            [
                pd.DataFrame(old_trades),
                new_trades_df[new_trades_df["exit_time"] > pd.to_datetime(last_old_ts, unit="ms", utc=True)],
            ],
            ignore_index=True,
        ) if not new_trades_df.empty else pd.DataFrame(old_trades)

        print(
            f"Combined: {len(old_trades)} old trades + "
            f"{len(combined_trades) - len(old_trades)} new trades"
        )
    else:
        combined_trades = new_trades
        combined_equity_points = new_equity_points
        combined_equity_df = new_equity_df
        combined_trades_df = new_trades_df

    # The allocated capital reference stays the original trading capital.
    kpis = store.compute_kpis(combined_trades, combined_equity_points, capital)
    kpis["total_account_equity"] = kpis["final_equity"]
    kpis["allocated_capital"] = capital
    kpis["return_on_total_equity"] = kpis["total_return"]

    metrics = compute_metrics(combined_equity_df, combined_trades_df, capital)

    payload = {
        "variant": variant,
        "params": {**COMMON_PARAMS, **params},
        "capital": capital,
        "total_account_equity": capital,
        "risk_per_trade_pct": None if USE_FIXED_RISK else RISK_PER_TRADE_PCT,
        "fixed_risk_amount": capital * RISK_PER_TRADE_PCT if USE_FIXED_RISK else None,
        "daily_loss_cap_pct": None,
        "fixed_daily_loss_cap": capital * RISK_PER_TRADE_PCT * DAILY_LOSS_CAP_MULTIPLIER if USE_FIXED_RISK else capital * 0.003,
        "daily_loss_cap_multiplier": DAILY_LOSS_CAP_MULTIPLIER,
        "kpis": kpis,
        "metrics": metrics,
        "equity": {"ts": [p[0] for p in combined_equity_points], "values": [p[1] for p in combined_equity_points]},
        "trades": combined_trades,
    }

    out_path = DATA_DIR / f"fvg_backtest_{variant}.json"
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    elapsed = time.time() - t0
    print(
        f"  -> {len(combined_trades)} total trades, final equity ${kpis['final_equity']:.2f}, "
        f"Sharpe {kpis['sharpe_ratio']:.2f}, max DD {kpis['max_drawdown']:.2f}% "
        f"({elapsed:.1f}s)"
    )
    print(f"  -> saved {out_path}")
    return payload


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    store.init()

    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    capital = TRADING_CAPITAL
    print(f"Trading capital for single tight variant: ${capital:,.2f}")

    for variant, params in VARIANTS.items():
        _run_variant(client, variant, params, capital)


if __name__ == "__main__":
    main()
