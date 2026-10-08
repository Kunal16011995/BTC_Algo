"""
Minimal signed REST client for Bybit v5.

Private calls (orders, positions, balance) go to the demo host and are
signed with HMAC-SHA256. Public market data (klines, instrument info) goes
to the standard public host and needs no signature.

Signing scheme (v5):
    signature = hex(HMAC_SHA256(secret, timestamp + api_key + recv_window + payload))
    payload   = query string (GET) or exact JSON body string (POST)
"""

import hashlib
import hmac
import json
import logging
import time

import requests

log = logging.getLogger('bot.bybit_client')


class BybitClient:
    def __init__(self, api_key, api_secret, demo_base_url, public_base_url):
        self.api_key = api_key
        self.api_secret = api_secret
        self.demo_base_url = demo_base_url.rstrip('/')
        self.public_base_url = public_base_url.rstrip('/')
        self.max_rate_limit_retries = 6
        self.recv_window = '5000'
        self.session = requests.Session()
        # Bybit's CDN rejects the default python-requests User-Agent with a
        # non-JSON error page on some public endpoints.
        self.session.headers.update({'User-Agent': 'BTC-momentum-bot/1.0'})
        # Sync local clock to Bybit server time so signed requests don't fail
        # when the PC clock is off.
        self._time_offset_ms = 0
        self._time_offset_ms = self._sync_server_time()

    def _sync_server_time(self):
        try:
            data = self._request('GET', '/v5/market/time', self.public_base_url, auth=False)
            time_nano = int(data['result']['timeNano'])
            server_ms = time_nano // 1_000_000
            local_ms = int(time.time() * 1000)
            offset = server_ms - local_ms
            log.info('Bybit server time synced, offset=%d ms', offset)
            return offset
        except Exception as exc:
            log.warning('Could not sync Bybit server time (%s); using local time', exc)
            return 0

    # ------------------------------------------------------------------
    # Low-level request handling
    # ------------------------------------------------------------------
    def _sign(self, payload):
        return hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def _request(self, method, path, base_url, params=None, body=None, auth=True):
        timestamp = str(int(time.time() * 1000) + self._time_offset_ms)
        query = '&'.join(f'{k}={v}' for k, v in (params or {}).items())
        url = f'{base_url}{path}'
        if method == 'GET' and query:
            url = f'{url}?{query}'
        body_str = json.dumps(body, separators=(',', ':')) if body is not None else ''

        headers = {'Content-Type': 'application/json'}
        if auth:
            payload = timestamp + self.api_key + self.recv_window
            payload += query if method == 'GET' else body_str
            headers.update({
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': self.recv_window,
                'X-BAPI-SIGN': self._sign(payload),
            })

        delay = 5.0
        for attempt in range(1, self.max_rate_limit_retries + 1):
            try:
                if method == 'GET':
                    resp = self.session.get(url, headers=headers, timeout=30)
                elif method == 'POST':
                    resp = self.session.post(url, headers=headers, data=body_str, timeout=30)
                else:
                    raise ValueError(f'Unsupported method {method}')
            except requests.exceptions.RequestException:
                # Network-level failure: back off briefly and retry.
                if attempt < self.max_rate_limit_retries:
                    log.warning('%s %s network error, retrying in %.0fs (attempt %d/%d)',
                                method, path, delay, attempt, self.max_rate_limit_retries)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

            data = resp.json()
            if data.get('retCode') == 10006:  # rate limit: back off and retry
                if attempt < self.max_rate_limit_retries:
                    log.warning('%s %s rate limited (10006), retrying in %.0fs (attempt %d/%d)',
                                method, path, delay, attempt, self.max_rate_limit_retries)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)  # cap backoff at 60s
                    continue
                raise RuntimeError(f'Bybit {method} {path} still rate limited after '
                                   f'{self.max_rate_limit_retries} retries')
            if data.get('retCode') != 0:
                raise RuntimeError(f"Bybit {method} {path} error {data.get('retCode')}: {data.get('retMsg')}")
            return data
        raise RuntimeError(f'Bybit {method} {path}: unreachable code path')

    # ------------------------------------------------------------------
    # Public market data (no auth)
    # ------------------------------------------------------------------
    def get_klines(self, symbol, interval, limit=200, start_ms=None, end_ms=None):
        """Return raw kline rows (newest first): [openTime, O, H, L, C, volume, turnover]."""
        params = {'category': 'linear', 'symbol': symbol, 'interval': str(interval), 'limit': limit}
        if start_ms:
            params['start'] = str(start_ms)
        if end_ms:
            params['end'] = str(end_ms)
        data = self._request('GET', '/v5/market/kline', self.public_base_url, params=params, auth=False)
        return data['result']['list']

    def get_instrument(self, symbol):
        params = {'category': 'linear', 'symbol': symbol}
        data = self._request('GET', '/v5/market/instruments-info', self.public_base_url, params=params, auth=False)
        return data['result']['list'][0]

    # ------------------------------------------------------------------
    # Private (demo account, signed)
    # ------------------------------------------------------------------
    def get_wallet_balance(self):
        """Return the first account entry (totalEquity etc.). Tries UNIFIED then CONTRACT."""
        for account_type in ('UNIFIED', 'CONTRACT'):
            try:
                data = self._request(
                    'GET', '/v5/account/wallet-balance', self.demo_base_url,
                    params={'accountType': account_type},
                )
            except RuntimeError:
                continue
            accounts = data['result']['list']
            if accounts:
                return accounts[0]
        raise RuntimeError('Could not read wallet balance on UNIFIED or CONTRACT account')

    def get_position(self, symbol):
        """Return the position entry for symbol, or None when flat."""
        data = self._request(
            'GET', '/v5/position/list', self.demo_base_url,
            params={'category': 'linear', 'symbol': symbol},
        )
        positions = data['result']['list']
        return positions[0] if positions else None

    def set_leverage(self, symbol, leverage):
        body = {
            'category': 'linear',
            'symbol': symbol,
            'buyLeverage': str(leverage),
            'sellLeverage': str(leverage),
        }
        try:
            return self._request('POST', '/v5/position/set-leverage', self.demo_base_url, body=body)
        except RuntimeError as exc:
            # 110043 = leverage already at this value; 10005 = key lacks the
            # permission -> keep the account's current leverage and continue.
            if '110043' in str(exc):
                return None
            if '10005' in str(exc):
                print(f'Note: API key cannot set leverage ({exc}); using account default.')
                return None
            raise

    def place_market_order(self, symbol, side, qty, stop_loss=None, take_profit=None,
                            trailing_stop=None, active_price=None):
        """side: 'Buy' or 'Sell'. Returns the raw order result.

        Optional stop_loss, take_profit, and trailing_stop are attached
        server-side in Full tpslMode (whole position). Use active_price to
        activate the trailing stop only after a target is reached.
        """
        def _fmt(val):
            return f'{val:f}'.rstrip('0').rstrip('.') if isinstance(val, float) else str(val)

        body = {
            'category': 'linear',
            'symbol': symbol,
            'side': side,
            'orderType': 'Market',
            'qty': _fmt(qty),
            'positionIdx': 0,  # one-way mode
            'tpslMode': 'Full',
        }
        if stop_loss is not None:
            body['stopLoss'] = _fmt(stop_loss)
            body['slTriggerBy'] = 'LastPrice'
        if take_profit is not None:
            body['takeProfit'] = _fmt(take_profit)
            body['tpTriggerBy'] = 'LastPrice'
        if trailing_stop is not None:
            body['trailingStop'] = _fmt(trailing_stop)
        if active_price is not None:
            body['activePrice'] = _fmt(active_price)
        return self._request('POST', '/v5/order/create', self.demo_base_url, body=body)['result']

    def set_trading_stop(self, symbol, stop_loss=None, trailing_stop=None, active_price=None):
        """Attach/update a server-side stop-loss or trailing stop on the open position."""
        body = {
            'category': 'linear',
            'symbol': symbol,
            'positionIdx': 0,
        }
        if stop_loss is not None:
            body['stopLoss'] = f'{stop_loss:f}'.rstrip('0').rstrip('.') if isinstance(stop_loss, float) else str(stop_loss)
            body['slTriggerBy'] = 'LastPrice'
        if trailing_stop is not None:
            body['trailingStop'] = f'{trailing_stop:f}'.rstrip('0').rstrip('.') if isinstance(trailing_stop, float) else str(trailing_stop)
        if active_price is not None:
            body['activePrice'] = f'{active_price:f}'.rstrip('0').rstrip('.') if isinstance(active_price, float) else str(active_price)
        return self._request('POST', '/v5/position/trading-stop', self.demo_base_url, body=body)['result']

    def get_last_price(self, symbol):
        """Current last traded price for a linear symbol."""
        data = self._request(
            'GET', '/v5/market/tickers', self.public_base_url,
            params={'category': 'linear', 'symbol': symbol}, auth=False,
        )
        return float(data['result']['list'][0]['lastPrice'])

    def get_closed_pnl(self, symbol, limit=100):
        """Return closed-PnL records (entry/exit prices, realized PnL per closed trade)."""
        data = self._request(
            'GET', '/v5/position/closed-pnl', self.demo_base_url,
            params={'category': 'linear', 'symbol': symbol, 'limit': limit},
        )
        return data['result']['list']
