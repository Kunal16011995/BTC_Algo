import MetaTrader5 as mt5
from datetime import datetime


def get_live_price(symbol: str = "XAUUSD") -> dict | None:
    """Fetch the latest live price for a symbol from MT5."""
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {mt5.last_error()}")

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")

        return {
            "symbol": symbol,
            "time": datetime.fromtimestamp(tick.time),
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": round(tick.ask - tick.bid, 6),
            "last": tick.last,
            "volume": tick.volume,
        }
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    price = get_live_price("XAUUSD")
    print(price)
