# BTC FVG Momentum Bot — Changelog

## 2026-11-16 — Live bot switched to limit orders at FVG midpoint

### Changed
- `src/fvg/bot.py`
  - `_place_live_trade` now places a **GTC limit order at the FVG midpoint** instead of a market order.
  - Added `pending_live_orders` tracker stored in `bot.db`.
  - Each bar the bot polls open orders: filled orders are promoted to live trades; expired orders are cancelled after `fvg_lookforward` bars (8 bars / 2 hours).
  - Skips new setups if a pending order or open position already exists.
- `src/shared/bybit_client.py`
  - Added `place_limit_order`, `get_open_orders`, `get_order`, `cancel_order`.

### Why
- Market-order fills were 200–700 points away from the FVG midpoint, turning a +412% theoretical backtest into a ~−90% live result.
- Limit orders at the midpoint align live execution with the profitable backtest assumption.

### Risk
- Not every FVG midpoint will be revisited in live markets, so fill rate and trade count will be lower than the backtest.

---

## 2026-11-16 — Backtest aligned to limit-order FVG midpoint assumption

### Changed
- `src/fvg/backtest.py`
  - Entry is again at the **FVG midpoint** (limit-order assumption).
  - Realistic Bybit taker fees **0.055% on entry and exit**.
- `src/fvg/compute_backtest.py`
  - Fees updated to 0.055% both sides.

### Results (full Bybit history 2020-03-25 → 2026-11-16)
- 1,561 trades
- Final equity: **$473,967.32** (+412.66%)
- Sharpe: **3.71**
- Max drawdown: **−3.11%**

---

## 2026-11-16 — Full-history backtest run

### Changed
- Ran backtest from the earliest available Bybit BTCUSDT data (2020-03-25) instead of 2025-01-01.

---

## 2026-11-13 — Live safety fixes for orphan positions and missing SL/TP

### Changed
- `src/fvg/bot.py`
  - Added **startup orphan guard**: if Bybit has an open position but `open_live_trades` is empty, close it immediately.
  - Added **in-loop orphan guard**: every bar, an untracked live position is closed.
  - Added **post-entry SL/TP verification**: after a live order fills, the bot checks the position and re-sets hard SL/TP if missing.
  - Added `pending_close` flag when `_close_live_position_immediately` runs, so ghost trades are not managed further.
  - `_match_live_trade` now matches closed-PnL by the stored close order ID.
- `src/shared/bybit_client.py`
  - `set_trading_stop` now accepts an optional `take_profit` parameter.

### Why
- An untracked 1.588 BTC long position was left open with no stop-loss and no take-profit, losing ~$4,600.
- Root cause: a closed short trade stayed in the tracker, its trailing SL kept updating, and a later "close" order accidentally opened a new long.

---

## 2026-11-13 — VPS clock-drift fix

### Changed
- `src/shared/bybit_client.py`
  - Widened `recv_window` from 5,000 ms to **30,000 ms** to tolerate VPS clock drift relative to Bybit.

---

## 2026-11-13 — Telegram 400 Bad Request fix

### Changed
- `src/shared/notify.py`
  - HTML-escape notification text before sending, restoring `<b>` tags afterwards.

### Why
- No-setup reason strings contained `<=`, which Telegram HTML parse mode rejected with HTTP 400.

---

## 2026-11-13 — Ops tooling

### Added
- `scripts/audit_live_trade.py` — inspect current Bybit position and recent order history.
- `docs/BOT_COMMANDS.txt` — command reference for VPS and local operations.
- `scripts/fetch_bybit_trades.py` — fetch Bybit closed-PnL for reconciliation.
- `scripts/query_bot_db.py` — query bot.db trades and meta keys.
