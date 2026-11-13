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
        # A wider recv_window tolerates VPS clock drift relative to Bybit.
        # 30s is still safe and prevents timestamp-drift rejects when the
        # server clock is a few seconds ahead of the VPS clock.
        self.recv_window = '30000'
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
            if data.get('retCode') == 10002:  # timestamp/recv_window drift: resync and retry once
                if attempt < self.max_rate_limit_retries:
                    log.warning('%s %s timestamp drift (10002), resyncing server time (attempt %d/%d)',
                                method, path, attempt, self.max_rate_limit_retries)
                    self._time_offset_ms = self._sync_server_time()
                    time.sleep(0.5)
                    continue
                raise RuntimeError(f"Bybit {method} {path} timestamp drift after "
                                   f"{self.max_rate_limit_retries} retries: {data.get('retMsg')}")
            if data.get('retCode') != 0:
                raise RuntimeError(f"Bybit {method} {path} error {data.get('retCode')}: {data.get('retMsg')}")
            return data
        raise RuntimeError(f'Bybit {method} {path}: unreachable code path')

    # ------------------------------------------------------------------
    # Public market data (no auth)
    # ------------------------------------------------------------------
    def get_klines(self, symbol, interval, limit=200, start_ms=None, end_ms=None, base_url=None):
        """Return raw kline rows (newest first): [openTime, O, H, L, C, volume, turnover]."""
        params = {'category': 'linear', 'symbol': symbol, 'interval': str(interval), 'limit': limit}
        if start_ms:
            params['start'] = str(start_ms)
        if end_ms:
            params['end'] = str(end_ms)
        base_url = base_url or self.public_base_url
        data = self._request('GET', '/v5/market/kline', base_url, params=params, auth=False)
        return data['result']['list']

    def get_all_klines(self, symbol, interval, start_ms=None, end_ms=None):
        """Fetch every available kline row for a symbol/interval.

        Bybit limits each /v5/market/kline request to 1000 rows and a 7-day
        time range, so this helper paginates in chunks and returns a single
        list ordered newest-first (Bybit's native order).
        """
        all_rows = []
        page = 0
        # If no explicit range, fetch as far back as Bybit allows and up to now.
        if end_ms is None:
            end_ms = int(time.time() * 1000)
        # Use 5-day chunks to stay safely inside Bybit's 7-day limit and reduce
        # the chance of transient 10016 service errors on long historical ranges.
        chunk_ms = 5 * 24 * 60 * 60 * 1000
        chunk_end = end_ms
        while True:
            chunk_start = max(start_ms or 0, chunk_end - chunk_ms)
            page += 1

            # Retry transient kline service errors with exponential backoff.
            rows = None
            delay = 1.0
            for attempt in range(1, 6):
                try:
                    # Use the demo host for bulk historical klines; it is more
                    # tolerant of long date-range queries than the public host.
                    rows = self.get_klines(
                        symbol, interval, limit=1000,
                        start_ms=chunk_start, end_ms=chunk_end,
                        base_url=self.demo_base_url,
                    )
                    break
                except RuntimeError as exc:
                    if '10016' in str(exc) and attempt < 5:
                        log.warning('Kline service error on page %d (attempt %d/5), retrying in %.0fs: %s',
                                    page, attempt, delay, exc)
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise

            if rows:
                all_rows.extend(rows)
                log.info('Fetched %d %s kline rows (page %d)', len(all_rows), interval, page)
            if start_ms is not None and chunk_start <= start_ms:
                break
            if not rows and chunk_start <= start_ms:
                break
            if chunk_start <= 0:
                break
            chunk_end = chunk_start - 1
            time.sleep(0.5)  # be polite to public endpoint
        return all_rows

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

    def set_trading_stop(self, symbol, stop_loss=None, take_profit=None, trailing_stop=None, active_price=None):
        """Attach/update a server-side stop-loss, take-profit or trailing stop on the open position."""
        body = {
            'category': 'linear',
            'symbol': symbol,
            'positionIdx': 0,
        }
        if stop_loss is not None:
            body['stopLoss'] = f'{stop_loss:f}'.rstrip('0').rstrip('.') if isinstance(stop_loss, float) else str(stop_loss)
            body['slTriggerBy'] = 'LastPrice'
        if take_profit is not None:
            body['takeProfit'] = f'{take_profit:f}'.rstrip('0').rstrip('.') if isinstance(take_profit, float) else str(take_profit)
            body['tpTriggerBy'] = 'LastPrice'
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

    @staticmethod
    def _chunk_time_range(start_ms, end_ms, max_span_ms=7 * 24 * 60 * 60 * 1000):
        """Split [start_ms, end_ms] into chunks no wider than max_span_ms."""
        if start_ms is None or end_ms is None:
            return [(start_ms, end_ms)]
        chunks = []
        chunk_start = start_ms
        while chunk_start < end_ms:
            chunk_end = min(chunk_start + max_span_ms, end_ms)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end
        return chunks

    def get_closed_pnl(self, symbol, limit=100, cursor=None, start_ms=None, end_ms=None):
        """Return one page of closed-PnL records (entry/exit prices, realized PnL per closed trade)."""
        params = {'category': 'linear', 'symbol': symbol, 'limit': limit}
        if cursor:
            params['cursor'] = cursor
        if start_ms:
            params['startTime'] = str(start_ms)
        if end_ms:
            params['endTime'] = str(end_ms)
        data = self._request(
            'GET', '/v5/position/closed-pnl', self.demo_base_url,
            params=params,
        )
        return data['result']['list'], data['result'].get('nextPageCursor')

    def get_all_closed_pnl(self, symbol, start_ms=None, end_ms=None):
        """Fetch every closed-PnL record for symbol and return as a list.

        Automatically splits requests wider than 7 days to comply with Bybit's
        maximum time-range limit.
        """
        all_pnl = []
        for chunk_start, chunk_end in self._chunk_time_range(start_ms, end_ms):
            cursor = None
            page = 0
            while True:
                page += 1
                pnl, cursor = self.get_closed_pnl(symbol, limit=100, cursor=cursor,
                                                  start_ms=chunk_start, end_ms=chunk_end)
                if not pnl:
                    break
                all_pnl.extend(pnl)
                log.info('Fetched %d closed-PnL records (page %d)', len(all_pnl), page)
                if not cursor:
                    break
        return all_pnl

    def get_order_history(self, symbol, limit=50, cursor=None, start_ms=None, end_ms=None):
        """Return one page of historical orders for a symbol."""
        params = {'category': 'linear', 'symbol': symbol, 'limit': limit}
        if cursor:
            params['cursor'] = cursor
        if start_ms:
            params['startTime'] = str(start_ms)
        if end_ms:
            params['endTime'] = str(end_ms)
        data = self._request(
            'GET', '/v5/order/history', self.demo_base_url,
            params=params,
        )
        return data['result']['list'], data['result'].get('nextPageCursor')

    def get_all_order_history(self, symbol, start_ms=None, end_ms=None):
        """Fetch every historical order for symbol and return as a list.

        Automatically splits requests wider than 7 days to comply with Bybit's
        maximum time-range limit.
        """
        all_orders = []
        for chunk_start, chunk_end in self._chunk_time_range(start_ms, end_ms):
            cursor = None
            page = 0
            while True:
                page += 1
                orders, cursor = self.get_order_history(symbol, limit=50, cursor=cursor,
                                                        start_ms=chunk_start, end_ms=chunk_end)
                if not orders:
                    break
                all_orders.extend(orders)
                log.info('Fetched %d historical orders (page %d)', len(all_orders), page)
                if not cursor:
                    break
        return all_orders

    def get_executions(self, symbol, limit=100, cursor=None, start_ms=None, end_ms=None):
        """Return one page of execution/fill records for a symbol.

        Use the returned 'cursor' from data['result']['nextPageCursor'] to
        paginate backwards through history.
        """
        params = {'category': 'linear', 'symbol': symbol, 'limit': limit}
        if cursor:
            params['cursor'] = cursor
        if start_ms:
            params['startTime'] = str(start_ms)
        if end_ms:
            params['endTime'] = str(end_ms)
        data = self._request(
            'GET', '/v5/execution/list', self.demo_base_url,
            params=params,
        )
        return data['result']['list'], data['result'].get('nextPageCursor')

    def get_all_executions(self, symbol, start_ms=None, end_ms=None):
        """Fetch every execution record for symbol and return as a list.

        Automatically splits requests wider than 7 days to comply with Bybit's
        maximum time-range limit.
        """
        all_execs = []
        for chunk_start, chunk_end in self._chunk_time_range(start_ms, end_ms):
            cursor = None
            page = 0
            while True:
                page += 1
                execs, cursor = self.get_executions(symbol, limit=100, cursor=cursor,
                                                    start_ms=chunk_start, end_ms=chunk_end)
                if not execs:
                    break
                all_execs.extend(execs)
                log.info('Fetched %d execution records (page %d)', len(all_execs), page)
                if not cursor:
                    break
        return all_execs
