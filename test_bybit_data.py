import os
import time
import hmac
import hashlib
import requests
from dotenv import load_dotenv
from urllib.parse import urlencode

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL = os.getenv("BYBIT_DEMO_BASE_URL", "https://api-demo.bybit.com")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")

def get_public_klines(symbol=SYMBOL, interval="60", limit=10):
    url = f"{BASE_URL}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data}")
    return data["result"]["list"]

def get_wallet_balance():
    url = f"{BASE_URL}/v5/account/wallet-balance"
    timestamp = str(int(time.time() * 1000))
    params = {"accountType": "UNIFIED"}
    param_str = timestamp + API_KEY + "5000" + urlencode(sorted(params.items()))
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": "5000",
    }
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data}")
    return data["result"]

if __name__ == "__main__":
    print(f"Base URL: {BASE_URL}")
    print(f"Symbol: {SYMBOL}")
    print("\n--- Public kline test ---")
    klines = get_public_klines()
    print(f"Received {len(klines)} candles")
    print("Most recent candle:", klines[0])

    print("\n--- Private wallet balance test ---")
    balance = get_wallet_balance()
    print("Wallet balance response:", balance)
