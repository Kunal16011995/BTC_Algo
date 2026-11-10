#!/usr/bin/env python3
"""Query the bot.db trades and meta tables."""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "bot.db"


def list_trades(variant: str | None = None, limit: int = 30):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if variant:
        cur.execute(
            "SELECT exit_ts, side, qty, entry_price, exit_price, pnl, variant FROM trades "
            "WHERE variant=? ORDER BY exit_ts DESC LIMIT ?",
            (variant, limit),
        )
    else:
        cur.execute(
            "SELECT exit_ts, side, qty, entry_price, exit_price, pnl, variant FROM trades "
            "ORDER BY exit_ts DESC LIMIT ?",
            (limit,),
        )
    print(f"{'Exit Time (UTC)':<20} {'Side':<6} {'Qty':<10} {'Entry':<14} {'Exit':<14} {'PnL':<12} {'Variant'}")
    print("-" * 100)
    for row in cur.fetchall():
        ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{ts:<20} {row[1]:<6} {row[2]:<10.3f} {row[3]:<14.2f} {row[4]:<14.2f} {row[5]:<12.2f} {row[6]}"
        )


def list_meta():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM meta")
    print("Meta keys:")
    for key, value in cur.fetchall():
        print(f"  {key}: {value[:200]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--trades", action="store_true", help="List trades")
    parser.add_argument("--meta", action="store_true", help="List meta keys")
    parser.add_argument("--variant", type=str, default="tight", help="Filter trades by variant")
    parser.add_argument("--limit", type=int, default=30, help="Number of trades")
    args = parser.parse_args()

    if not (args.trades or args.meta):
        args.trades = True

    if args.trades:
        list_trades(args.variant, args.limit)
        print()
    if args.meta:
        list_meta()
