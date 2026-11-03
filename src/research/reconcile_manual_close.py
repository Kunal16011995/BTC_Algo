"""Reconcile a manually-closed live position with the local dashboard state.

Use this when you close a Bybit position manually and the forward-test
dashboard still shows it as OPEN because the bot's automatic reconciliation
did not match the closed-PnL row.

The script:
1. Checks the actual Bybit position (must be flat).
2. Fetches recent closed-PnL rows.
3. Matches them to stored open_live_trades.
4. Records any matched closes in bot.db and updates the equity curve.
5. Clears any stale open_live_trades that are no longer open on Bybit.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared import config
from src.shared import store
from src.shared.bybit_client import BybitClient

SYMBOL = "BTCUSDT"
VARIANT = "tight"


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _infer_position_side(closed_row: dict) -> str:
    """Same logic as store._infer_position_side but using closedPnl."""
    qty = float(closed_row.get("qty", 0) or 0)
    entry = float(closed_row.get("avgEntryPrice", 0) or 0)
    exit_ = float(closed_row.get("avgExitPrice", 0) or 0)
    pnl = float(closed_row.get("closedPnl", 0) or 0)
    if qty <= 0 or entry <= 0 or exit_ <= 0:
        return closed_row.get("side", "")
    gross_long = (exit_ - entry) * qty
    fees = (entry + exit_) * qty * store.TAKER_FEE
    err_long = abs(gross_long - fees - pnl)
    err_short = abs(-gross_long - fees - pnl)
    return "Buy" if err_long <= err_short else "Sell"


def _match_live_trade(closed_row: dict, open_trades: list[dict]) -> dict | None:
    """Match a closed-PnL row to a stored open live trade."""
    try:
        close_side = closed_row.get("side", "")
        qty = float(closed_row.get("qty", 0) or 0)
        entry_price = float(closed_row.get("avgEntryPrice", 0) or 0)
        updated_ms = int(closed_row.get("updatedTime", 0) or 0)
    except Exception:
        return None

    best_match = None
    best_score = float("inf")

    for trade in open_trades:
        # Closing side must be opposite of opening side
        if trade.get("side") == "Buy" and close_side != "Sell":
            continue
        if trade.get("side") == "Sell" and close_side != "Buy":
            continue

        # Quantity match
        qty_diff = abs(float(trade.get("qty", 0) or 0) - qty)

        # Entry price match (allow up to 5% slippage for manual closes)
        entry = float(trade.get("entry_price", 0) or 0)
        entry_tol = max(entry, entry_price) * 0.05
        price_diff = abs(entry - entry_price)

        # Exit must be after entry
        if updated_ms < int(trade.get("entry_ts_ms", 0) or 0):
            continue

        # Score: lower is better
        score = qty_diff + price_diff
        if qty_diff <= 0.01 and (entry <= 0 or entry_price <= 0 or price_diff <= entry_tol):
            if score < best_score:
                best_score = score
                best_match = trade

    return best_match


def main():
    client = BybitClient(
        config.BYBIT_API_KEY,
        config.BYBIT_API_SECRET,
        config.BYBIT_DEMO_BASE_URL,
        config.BYBIT_PUBLIC_BASE_URL,
    )

    store.init()

    pos = client.get_position(SYMBOL)
    current_size = abs(float(pos.get("size", 0) or 0)) if pos else 0.0
    print(f"[{_now_str()}] Current Bybit position size: {current_size:.3f} BTC")

    open_live_trades = json.loads(store.get_meta("open_live_trades", "[]") or "[]")
    print(f"[{_now_str()}] Stored open_live_trades: {len(open_live_trades)}")
    for t in open_live_trades:
        print(
            f"  - {t.get('side')} {t.get('qty')} BTC @ {t.get('entry_price')} "
            f"(order_id={t.get('order_id')})"
        )

    if current_size > 0 and not open_live_trades:
        print("\nWARNING: Bybit shows an open position but no stored open_live_trades.")
        print("Run fix_current_position.py instead to seed the open trade.")
        return

    closed = client.get_all_closed_pnl(SYMBOL)
    print(f"\n[{_now_str()}] Fetched {len(closed)} closed-PnL row(s)")

    # Load capital for the variant
    allocated = 92_453.04
    capital = store.rebuild_variant_equity_curve(VARIANT, allocated, store.load_trades_variant(VARIANT))
    print(f"[{_now_str()}] Current {VARIANT} capital before reconciliation: ${capital:,.2f}")

    matched = 0
    recorded = 0
    unmatched_closes = []

    for row in closed:
        trade = _match_live_trade(row, open_live_trades)
        if trade is None:
            continue

        matched += 1
        print(
            f"\n  MATCHED closed-PnL row: side={row.get('side')} qty={row.get('qty')} "
            f"entry={row.get('avgEntryPrice')} exit={row.get('avgExitPrice')} "
            f"pnl={row.get('closedPnl')} updatedTime={row.get('updatedTime')}"
        )

        try:
            # Make sure the row has the inferred side in raw so store.upsert_trade works consistently
            row_copy = dict(row)
            row_copy["side"] = _infer_position_side(row_copy)
            store.upsert_trade(row_copy, variant=VARIANT)
        except Exception as exc:
            print(f"  ERROR recording trade: {exc}")
            continue

        pnl = float(row.get("closedPnl", 0) or 0)
        close_ts_ms = int(row.get("updatedTime", 0) or 0)
        capital += pnl
        if close_ts_ms:
            store.append_variant_equity_point(VARIANT, close_ts_ms / 1000, capital)

        open_live_trades.remove(trade)
        recorded += 1
        print(f"  RECORDED: pnl=${pnl:,.2f}, new capital=${capital:,.2f}")

    # If Bybit is flat but we still have stored open trades, they are stale.
    if current_size == 0 and open_live_trades:
        print(f"\n[{_now_str()}] Bybit is flat but {len(open_live_trades)} stored trade(s) remain.")
        print("Attempting to match remaining stale trades against closed-PnL...")

        # Try looser matching for manual closes where qty might differ slightly
        for trade in open_live_trades[:]:
            trade_qty = float(trade.get("qty", 0) or 0)
            trade_entry = float(trade.get("entry_price", 0) or 0)
            trade_side = trade.get("side", "")
            expected_close_side = "Sell" if trade_side == "Buy" else "Buy"

            for row in closed:
                if row.get("side") != expected_close_side:
                    continue
                row_qty = float(row.get("qty", 0) or 0)
                row_entry = float(row.get("avgEntryPrice", 0) or 0)
                # Loose qty tolerance (up to 10%) and price tolerance (up to 5%)
                if trade_qty > 0 and row_qty > 0 and abs(row_qty - trade_qty) / trade_qty <= 0.10:
                    if trade_entry > 0 and row_entry > 0 and abs(row_entry - trade_entry) / trade_entry <= 0.05:
                        print(
                            f"  LOOSE MATCH: {trade_side} {trade_qty} BTC -> closed row "
                            f"side={row.get('side')} qty={row_qty} pnl={row.get('closedPnl')}"
                        )
                        try:
                            row_copy = dict(row)
                            row_copy["side"] = _infer_position_side(row_copy)
                            store.upsert_trade(row_copy, variant=VARIANT)
                        except Exception as exc:
                            print(f"  ERROR recording loose match: {exc}")
                            continue

                        pnl = float(row.get("closedPnl", 0) or 0)
                        close_ts_ms = int(row.get("updatedTime", 0) or 0)
                        capital += pnl
                        if close_ts_ms:
                            store.append_variant_equity_point(VARIANT, close_ts_ms / 1000, capital)

                        if trade in open_live_trades:
                            open_live_trades.remove(trade)
                        recorded += 1
                        print(f"  RECORDED (loose): pnl=${pnl:,.2f}, new capital=${capital:,.2f}")
                        break

        if open_live_trades:
            print(f"\n  {len(open_live_trades)} stored trade(s) still unmatched. Removing stale entries.")
            for t in open_live_trades:
                print(f"    - removed stale {t.get('side')} {t.get('qty')} BTC @ {t.get('entry_price')}")
            open_live_trades = []

    store.set_meta("open_live_trades", json.dumps(open_live_trades, default=str))
    print(f"\n[{_now_str()}] Reconciliation complete.")
    print(f"  Matched closes: {matched}")
    print(f"  Recorded trades: {recorded}")
    print(f"  Remaining stored open trades: {len(open_live_trades)}")
    print(f"  Updated {VARIANT} capital: ${capital:,.2f}")


if __name__ == "__main__":
    main()
