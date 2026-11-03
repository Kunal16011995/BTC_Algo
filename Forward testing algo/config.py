"""
Bot configuration. All values can be overridden via .env.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Bybit environment (FVG momentum bot) ---
# Demo trading uses the dedicated demo host; public market data comes from the
# standard public host (same prices, no auth needed).
BYBIT_DEMO_BASE_URL = os.getenv('BYBIT_DEMO_BASE_URL', 'https://api-demo.bybit.com')
BYBIT_PUBLIC_BASE_URL = os.getenv('BYBIT_PUBLIC_BASE_URL', 'https://api.bybit.com')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')

# --- Bybit environment (WD Gann forward bot, separate account) ---
GANN_BYBIT_DEMO_BASE_URL = os.getenv('GANN_BYBIT_DEMO_BASE_URL', 'https://api-demo.bybit.com')
GANN_BYBIT_PUBLIC_BASE_URL = os.getenv('GANN_BYBIT_PUBLIC_BASE_URL', 'https://api.bybit.com')
GANN_BYBIT_API_KEY = os.getenv('GANN_BYBIT_API_KEY', '')
GANN_BYBIT_API_SECRET = os.getenv('GANN_BYBIT_API_SECRET', '')

# --- Market ---
SYMBOL = os.getenv('SYMBOL', 'BTCUSDT')          # USDT-margined linear perpetual
INTERVAL = os.getenv('INTERVAL', '30')           # Bybit kline minutes: 30 = 30m
LEVERAGE = float(os.getenv('LEVERAGE', '5'))

# --- CP momentum strategy (same defaults as the CP backtest) ---
SUPERTREND_FACTOR = float(os.getenv('SUPERTREND_FACTOR', '3.0'))
SUPERTREND_PERIOD = int(os.getenv('SUPERTREND_PERIOD', '10'))
MOMENTUM_LOOKBACK = int(os.getenv('MOMENTUM_LOOKBACK', '10'))
INITIAL_SL_POINTS = float(os.getenv('INITIAL_SL_POINTS', '500'))     # entry -> initial SL distance
TRAIL_OFFSET_POINTS = float(os.getenv('TRAIL_OFFSET_POINTS', '500')) # trail = extreme -/+ offset
TRAIL_ACTIVATION_POINTS = float(os.getenv('TRAIL_ACTIVATION_POINTS', '0'))
DIRECTION = os.getenv('DIRECTION', 'both')       # 'both', 'long_only', 'short_only'

# --- Risk management (strict 1%-risk sizing) ---
RISK_PER_TRADE_PCT = float(os.getenv('RISK_PER_TRADE_PCT', '1.0'))   # % of trading capital risked per trade
TRADING_CAPITAL_PCT = float(os.getenv('TRADING_CAPITAL_PCT', '20.0')) # only this % of total equity is used for trading
HISTORY_BARS = int(os.getenv('HISTORY_BARS', '300'))                 # bars loaded for signal calc
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '10'))                  # how often to check for a new bar
