#!/usr/bin/env python3
"""
Fetch closed-PnL trades from the Bybit demo account and print them in the same
format as the local backtest dashboard. Useful for reconciling forward-test
(VPS bot) trades against backtest trades.

Usage:
    python scripts/fetch_bybit_trades.py
    python scripts/fetch_bybit_trades.py --days 7
    python scripts/fetch_bybit_trades.py --csv bybit_trades.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.shared import config
from src.shared.bybit_client import BybitClient


def _side_from_order_type(order_type: str) -> str:
    return "BUY" if order_type.upper().startswith("BUY") else "SELL"


def _points(entry: float, exit: float, side: str) -> float:
    return entry - exit if side == "SELL" else exit - entry


def fetch_bybit_trades(symbol: str = "BTCUSDT", days: int | None = None):
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    if days:
        start_ms = int((datetime.now(timezone.utc).timestamp() - days * 24 * 60 * 60) * 1000)
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    else:
        start_ms = None
        end_ms = None

    records = client.get_all_closed_pnl(symbol, start_ms=start_ms, end_ms=end_ms)

    trades = []
    for r in records:
        side = _side_from_order_type(r.get("orderType", ""))
        qty = float(r.get("qty", 0) or 0)
        entry = float(r.get("avgEntryPrice", 0) or 0)
        exit_ = float(r.get("avgExitPrice", 0) or 0)
        pnl = float(r.get("closedPnl", 0) or 0)
        exit_ts_ms = int(r.get("updatedTime", 0) or 0)
        exit_dt = datetime.fromtimestamp(exit_ts_ms / 1000, tz=timezone.utc)
        trades.append(
            {
                "exit_time": exit_dt.strftime("%Y-%m-%d %H:%M"),
                "side": side,
                "qty": qty,
                "entry": entry,
                "exit": exit_,
                "points": _points(entry, exit_, side),
                "pnl": pnl,
                "exit_reason": r.get("exitType", ""),
            }
        )

    # Sort newest first
    trades.sort(key=lambda x: x["exit_time"], reverse=True)
    return trades


def print_trades(trades: list[dict]):
    if not trades:
        print("No closed-PnL trades found on the Bybit demo account.")
        return

    print(f"Bybit demo closed-PnL trades: {len(trades)} found")
    print()
    print(f"{'Exit Time (UTC)':<18} {'Side':<6} {'Qty (BTC)':<12} {'Entry':<14} {'Exit':<14} {'Points':<10} {'PnL (USD)':<12}")
    print("-" * 100)
    for t in trades:
        print(
            f"{t['exit_time']:<18} {t['side']:<6} {t['qty']:<12.3f} "
            f"{t['entry']:<14.2f} {t['exit']:<14.2f} {t['points']:<10.1f} {t['pnl']:<12.2f}"
        )


def export_csv(trades: list[dict], path: str):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["exit_time", "side", "qty", "entry", "exit", "points", "pnl", "exit_reason"],
        )
        writer.writeheader()
        writer.writerows(trades)
    print(f"Exported {len(trades)} trades to {path}")


def main():
    parser = argparse.ArgumentParser(description="Fetch Bybit demo closed-PnL trades")
    parser.add_argument("--days", type=int, default=None, help="Only fetch last N days")
    parser.add_argument("--csv", type=str, default=None, help="Export to CSV file")
    args = parser.parse_args()

    trades = fetch_bybit_trades(days=args.days)
    print_trades(trades)
    if args.csv:
        export_csv(trades, args.csv)


if __name__ == "__main__":
    main()
