"""One-time fix for the currently open live position.

- Removes the useless server-side trailing stop.
- Preserves hard SL and hard runner TP.
- Seeds the position into open_live_trades so the bot manages the trailing SL manually.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import store
from bybit_client import BybitClient

SYMBOL = "BTCUSDT"
TRADING_CAPITAL = 92_453.04
RISK_PER_TRADE = TRADING_CAPITAL * 0.005


def main():
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    pos = client.get_position(SYMBOL)
    if not pos or abs(float(pos.get("size", 0) or 0)) == 0:
        print("No open position found.")
        return

    side = pos.get("side")  # 'Buy' or 'Sell'
    size = float(pos.get("size", 0) or 0)
    entry = float(pos.get("avgPrice", 0) or 0)
    hard_sl = float(pos.get("stopLoss", 0) or 0)
    hard_tp = float(pos.get("takeProfit", 0) or 0)

    print(f"Open position: {side} {size} BTC @ {entry:.2f}")
    print(f"  Hard SL: {hard_sl:.2f}, Hard TP: {hard_tp:.2f}")

    # Remove server-side trailing stop (it is not honouring activation-at-TP1).
    try:
        client.set_trading_stop(SYMBOL, trailing_stop="0")
        print("Removed server-side trailing stop.")
    except Exception as exc:
        print(f"Could not remove trailing stop: {exc}")

    # Re-assert hard SL/TP so they are present and consistent.
    try:
        client.set_trading_stop(SYMBOL, stop_loss=hard_sl)
        print(f"Re-asserted hard SL at {hard_sl:.2f}.")
    except Exception as exc:
        print(f"Could not re-assert hard SL: {exc}")

    # Derive TP1 / trail distance from the current tight-variant parameters.
    # These match fvg_paper_bot.py VARIANTS['tight'].
    tp1_pct = 0.004
    if side == "Buy":
        tp1 = entry * (1.0 + tp1_pct)
    else:
        tp1 = entry * (1.0 - tp1_pct)
    trail_dist = abs(entry - tp1)

    trade = {
        "order_id": "manual-seed-" + str(int(entry)),
        "variant": "tight",
        "side": side,
        "qty": size,
        "entry_price": entry,
        "entry_ts_ms": int(pos.get("createdTime", 0) or 0),
        "stop_price": hard_sl,
        "tp1_price": tp1,
        "runner_max_price": hard_tp if hard_tp > 0 else entry,
        "trail_dist": trail_dist,
        "tp1_hit": False,
        "extreme_price": entry,
        "current_sl": hard_sl,
    }

    # Overwrite open_live_trades with this single seeded trade.
    store.set_meta("open_live_trades", json.dumps([trade], default=str))
    print("Seeded open_live_trades:")
    print(json.dumps(trade, indent=2, default=str))


if __name__ == "__main__":
    main()
