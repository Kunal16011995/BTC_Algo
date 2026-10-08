"""Manually record a closed live trade when Bybit's closed-PnL is missing/mismatched.

Run this and edit the TRADE dict below with the actual values from your manual close.
It will insert the trade into bot.db, update the equity curve, and clear the stale
open_live_trades entry.
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store

# ---------------------------------------------------------------------------
# EDIT THESE VALUES WITH YOUR ACTUAL MANUAL CLOSE DETAILS
# ---------------------------------------------------------------------------
TRADE = {
    "variant": "tight",
    "symbol": "BTCUSDT",
    # Opening side of the position (the side you entered with)
    "side": "Sell",  # or "Buy"
    "qty": 1.392,
    "entry_price": 82590.4,
    "exit_price": 82442.8,  # fill in your actual exit price
    "pnl": 579.95,
    # Exit timestamp in milliseconds (UTC). You can get this from Bybit order history.
    # If you don't know it, use current time: int(datetime.now(timezone.utc).timestamp() * 1000)
    "exit_ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
}
# ---------------------------------------------------------------------------


def main():
    store.init()

    # Clear stale open_live_trades
    open_live = json.loads(store.get_meta("open_live_trades", "[]") or "[]")
    if open_live:
        print(f"Clearing {len(open_live)} stale open_live_trades entry/entries.")
        store.set_meta("open_live_trades", "[]")

    # Rebuild capital from existing closed trades
    allocated = 92_453.04
    existing_trades = store.load_trades_variant(TRADE["variant"])
    capital = store.rebuild_variant_equity_curve(TRADE["variant"], allocated, existing_trades)
    print(f"Capital before recording manual close: ${capital:,.2f}")

    # Build the trade record in the same shape store.upsert_trade expects
    exit_ts = int(TRADE["exit_ts_ms"])
    trade_id = f"manual-{uuid.uuid4().hex[:12]}-{exit_ts}"

    # Determine closing side
    close_side = "Buy" if TRADE["side"] == "Sell" else "Sell"

    closed_row = {
        "orderId": trade_id,
        "updatedTime": exit_ts,
        "side": close_side,
        "symbol": TRADE["symbol"],
        "qty": TRADE["qty"],
        "avgEntryPrice": TRADE["entry_price"],
        "avgExitPrice": TRADE["exit_price"] if TRADE["exit_price"] else TRADE["entry_price"],
        "closedPnl": TRADE["pnl"],
    }

    store.upsert_trade(closed_row, variant=TRADE["variant"])

    capital += TRADE["pnl"]
    store.append_variant_equity_point(TRADE["variant"], exit_ts / 1000, capital)

    print(f"\nRecorded manual close:")
    print(f"  Variant:      {TRADE['variant']}")
    print(f"  Side (open):  {TRADE['side']}")
    print(f"  Qty:          {TRADE['qty']}")
    print(f"  Entry:        {TRADE['entry_price']}")
    print(f"  Exit:         {TRADE['exit_price'] if TRADE['exit_price'] else 'not provided'}")
    print(f"  PnL:          ${TRADE['pnl']:,.2f}")
    print(f"  Exit time:    {datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc)}")
    print(f"\nUpdated {TRADE['variant']} capital: ${capital:,.2f}")
    print("Refresh the dashboard (Ctrl+F5) to see the trade.")


if __name__ == "__main__":
    main()
