#!/usr/bin/env python3
"""Audit the current Bybit live position and the recent orders that created it.

Usage:
    python scripts/audit_live_trade.py
    python scripts/audit_live_trade.py --symbol BTCUSDT --days 3
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.shared import config
from src.shared.bybit_client import BybitClient

RELEVANT_ORDER_FIELDS = [
    "orderId",
    "symbol",
    "side",
    "orderType",
    "orderStatus",
    "qty",
    "cumExecQty",
    "avgPrice",
    "stopLoss",
    "takeProfit",
    "createdTime",
    "updatedTime",
]


def _fmt_ts(ms: int | str | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ms)


def main():
    parser = argparse.ArgumentParser(description="Audit live Bybit position and recent orders")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()

    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    pos = client.get_position(args.symbol)
    print("=== CURRENT POSITION ===")
    if pos:
        # Add human-readable timestamps
        readable = dict(pos)
        readable["createdTimeFmt"] = _fmt_ts(pos.get("createdTime"))
        readable["openTimeFmt"] = _fmt_ts(pos.get("openTime"))
        readable["updatedTimeFmt"] = _fmt_ts(pos.get("updatedTime"))
        print(json.dumps(readable, indent=2, default=str))
    else:
        print("No open position.")
    print()

    start_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000
    )
    orders = client.get_all_order_history(args.symbol, start_ms=start_ms)
    print(f"=== ORDER HISTORY (last {args.days} days, {len(orders)} orders) ===")

    # Show orders most likely to be the live entry (large qty market buy/sell)
    interesting = [
        o for o in orders
        if o.get("orderType") == "Market" and float(o.get("qty", 0) or 0) >= 0.5
    ]

    if not interesting:
        print("No large market orders found.")
        return

    for o in interesting:
        slim = {k: o.get(k) for k in RELEVANT_ORDER_FIELDS}
        slim["createdTimeFmt"] = _fmt_ts(o.get("createdTime"))
        slim["updatedTimeFmt"] = _fmt_ts(o.get("updatedTime"))
        print(json.dumps(slim, indent=2, default=str))
        print("-" * 60)


if __name__ == "__main__":
    main()
