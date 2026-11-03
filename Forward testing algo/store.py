"""
SQLite storage + KPI computation for the forward-testing dashboard.

The dashboard polls Bybit itself (closed-PnL for trades, wallet balance for
equity) and keeps local history here. All performance figures are computed
on the TRADING CAPITAL basis (TRADING_CAPITAL_PCT of total equity), since
only that slice is used for trading.

Paper forward-test additions (variant-aware trades + equity curve):
- trades.variant column stores the strategy variant ('base', 'tight', etc.).
- variant_equity_curve holds per-variant realized equity snapshots.
"""

import json
import os
import sqlite3
import threading

import numpy as np
import pandas as pd

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'bot.db')
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    variant TEXT,
    symbol TEXT,
    side TEXT,
    qty REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    exit_ts INTEGER,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS equity_curve (
    ts REAL PRIMARY KEY,
    equity REAL
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts REAL PRIMARY KEY,
    total_equity REAL,
    trading_equity REAL
);
CREATE TABLE IF NOT EXISTS variant_equity_curve (
    variant TEXT,
    ts REAL,
    equity REAL,
    PRIMARY KEY (variant, ts)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value REAL
);
"""


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _migrate():
    """Add variant column to pre-existing trades tables (idempotent)."""
    with _lock, _connect() as conn:
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN variant TEXT")
        except sqlite3.OperationalError:
            pass


def init():
    with _lock, _connect():
        pass
    _migrate()


# ----------------------------------------------------------------------
# Writes (called by the poller)
# ----------------------------------------------------------------------
TAKER_FEE = 0.00055  # Bybit linear perp taker fee, both sides


def _infer_position_side(trade):
    """Closed-PnL 'side' is the CLOSING order's side (Buy = cover a short).

    Infer the true position side by matching gross PnL +/- round-trip taker
    fees against the reported net PnL; funding (~$1) only matters when the
    move is near zero, where either reading is economically equivalent.
    """
    qty = float(trade.get('qty', 0) or 0)
    entry = float(trade.get('avgEntryPrice', 0) or 0)
    exit_ = float(trade.get('avgExitPrice', 0) or 0)
    pnl = float(trade.get('closedPnl', 0) or 0)
    if qty <= 0 or entry <= 0 or exit_ <= 0:
        return trade.get('side', '')
    gross_long = (exit_ - entry) * qty
    fees = (entry + exit_) * qty * TAKER_FEE
    err_long = abs(gross_long - fees - pnl)
    err_short = abs(-gross_long - fees - pnl)
    return 'Buy' if err_long <= err_short else 'Sell'


def upsert_trade(trade, variant=None):
    """Insert or update one closed-PnL record (keyed by orderId + updatedTime)."""
    key = f"{trade.get('orderId', '')}-{trade.get('updatedTime', '')}"
    side = _infer_position_side(trade)
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO trades (id, variant, symbol, side, qty, entry_price, exit_price, pnl, exit_ts, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   variant=excluded.variant, qty=excluded.qty, entry_price=excluded.entry_price,
                   exit_price=excluded.exit_price, pnl=excluded.pnl,
                   exit_ts=excluded.exit_ts, raw=excluded.raw""",
            (
                key, variant, trade.get('symbol', ''), side,
                float(trade.get('qty', 0) or 0), float(trade.get('avgEntryPrice', 0) or 0),
                float(trade.get('avgExitPrice', 0) or 0), float(trade.get('closedPnl', 0) or 0),
                int(trade.get('updatedTime', 0) or 0),
                json.dumps(trade),
            ),
        )


def upsert_trade_variant(trade):
    """Insert or update one paper-trade record keyed by trade['id'].

    `trade` must contain: id, variant, symbol, side, qty, entry_price,
    exit_price, pnl, exit_ts, raw.
    """
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO trades (id, variant, symbol, side, qty, entry_price, exit_price, pnl, exit_ts, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   variant=excluded.variant, symbol=excluded.symbol, side=excluded.side,
                   qty=excluded.qty, entry_price=excluded.entry_price,
                   exit_price=excluded.exit_price, pnl=excluded.pnl,
                   exit_ts=excluded.exit_ts, raw=excluded.raw""",
            (
                trade['id'], trade['variant'], trade.get('symbol', ''),
                trade.get('side', ''), float(trade.get('qty', 0) or 0),
                float(trade.get('entry_price', 0) or 0),
                float(trade.get('exit_price', 0) or 0),
                float(trade.get('pnl', 0) or 0),
                int(trade.get('exit_ts', 0) or 0),
                json.dumps(trade.get('raw', {})),
            ),
        )


def append_equity_point(ts, equity):
    with _lock, _connect() as conn:
        conn.execute(
            'INSERT INTO equity_curve (ts, equity) VALUES (?, ?) '
            'ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity',
            (ts, equity),
        )


def append_variant_equity_point(variant, ts, equity):
    """Append a realized equity snapshot for a paper-test variant.

    `ts` is in seconds (same convention as equity_curve).
    """
    with _lock, _connect() as conn:
        conn.execute(
            'INSERT INTO variant_equity_curve (variant, ts, equity) VALUES (?, ?, ?) '
            'ON CONFLICT(variant, ts) DO UPDATE SET equity=excluded.equity',
            (variant, ts, equity),
        )


def rebuild_variant_equity_curve(variant, base_capital, trades):
    """Recompute the variant equity curve from a list of closed trades.

    Returns the final equity.  Existing curve points for the variant are
    replaced so restarts do not accumulate stale or reset points.
    """
    equity = float(base_capital)
    points = []
    if trades:
        # Seed the starting capital at the moment just before the first trade.
        first_ts = int(trades[0].get('exit_ts', 0) or 0)
        if first_ts:
            points.append((first_ts / 1000.0 - 1.0, equity))
        for t in trades:
            pnl = float(t.get('pnl', 0) or 0)
            exit_ts = int(t.get('exit_ts', 0) or 0)
            equity += pnl
            if exit_ts:
                points.append((exit_ts / 1000.0, equity))
    else:
        # No trades yet: seed with starting capital at current time.
        import time
        points.append((time.time(), equity))

    with _lock, _connect() as conn:
        conn.execute('DELETE FROM variant_equity_curve WHERE variant = ?', (variant,))
        for ts, eq in points:
            conn.execute(
                'INSERT INTO variant_equity_curve (variant, ts, equity) VALUES (?, ?, ?) '
                'ON CONFLICT(variant, ts) DO UPDATE SET equity=excluded.equity',
                (variant, ts, eq),
            )
    return equity


def append_account_snapshot(ts, total_equity, trading_equity):
    with _lock, _connect() as conn:
        conn.execute(
            'INSERT INTO account_snapshots (ts, total_equity, trading_equity) VALUES (?, ?, ?) '
            'ON CONFLICT(ts) DO UPDATE SET total_equity=excluded.total_equity, '
            'trading_equity=excluded.trading_equity',
            (ts, total_equity, trading_equity),
        )


def get_meta(key, default=None):
    with _lock, _connect() as conn:
        row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default


def set_meta(key, value):
    with _lock, _connect() as conn:
        conn.execute(
            'INSERT INTO meta (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, value),
        )


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
def load_trades():
    with _lock, _connect() as conn:
        rows = conn.execute('SELECT * FROM trades ORDER BY exit_ts ASC').fetchall()
    return [dict(r) for r in rows]


def load_trades_variant(variant):
    with _lock, _connect() as conn:
        rows = conn.execute(
            'SELECT * FROM trades WHERE variant = ? ORDER BY exit_ts ASC', (variant,)
        ).fetchall()
    return [dict(r) for r in rows]


def load_equity_curve():
    with _lock, _connect() as conn:
        rows = conn.execute('SELECT ts, equity FROM equity_curve ORDER BY ts ASC').fetchall()
    return [(r['ts'], r['equity']) for r in rows]


def load_equity_curve_variant(variant):
    with _lock, _connect() as conn:
        rows = conn.execute(
            'SELECT ts, equity FROM variant_equity_curve WHERE variant = ? ORDER BY ts ASC',
            (variant,),
        ).fetchall()
    return [(r['ts'], r['equity']) for r in rows]


def load_account_snapshots():
    with _lock, _connect() as conn:
        rows = conn.execute(
            'SELECT ts, total_equity, trading_equity FROM account_snapshots ORDER BY ts ASC'
        ).fetchall()
    return [(r['ts'], r['total_equity'], r['trading_equity']) for r in rows]


# ----------------------------------------------------------------------
# KPI computation (mirrors the CP backtest dashboard semantics)
# ----------------------------------------------------------------------
def _daily_series(points):
    """Daily last equity values, forward-filled, from (ts, equity) points."""
    if len(points) < 2:
        return pd.Series(dtype=float)
    df = pd.DataFrame(points, columns=['ts', 'equity'])
    df['dt'] = pd.to_datetime(df['ts'], unit='s')
    df = df.set_index('dt')['equity']
    daily = df.resample('D').last().ffill().dropna()
    return daily


def compute_kpis(trades, equity_points, base_capital, account_points=None):
    """Return the KPI dict shown on the dashboard cards.

    Returns are computed on both bases:
    - trading capital (allocated slice, trade-built curve)
    - total account equity (mark-to-market of the whole account)
    """
    kpis = {
        'total_trades': len(trades),
        'win_rate': 0.0,
        'profit_factor': 0.0,
        'avg_monthly_return': 0.0,
        'max_drawdown': 0.0,
        'sharpe_ratio': 0.0,
        'avg_points_trailed': 0.0,
        'total_return': 0.0,
        'final_equity': float(base_capital),
        'base_capital': float(base_capital),
        'total_pnl': 0.0,
        # Trade-level KPIs
        'best_win': 0.0,
        'worst_loss': 0.0,
        'best_r': 0.0,
        'worst_r': 0.0,
        'avg_win': 0.0,
        'avg_loss': 0.0,
        'consecutive_wins': 0,
        'consecutive_losses': 0,
        # Whole-account basis
        'total_account_equity': 0.0,
        'allocated_capital': float(base_capital),
        'return_on_total_equity': 0.0,
    }

    if account_points:
        kpis['total_account_equity'] = float(account_points[-1][1])
        account_start = float(account_points[0][1])
        if account_start:
            kpis['return_on_total_equity'] = (float(account_points[-1][1]) / account_start - 1.0) * 100

    if not trades:
        return kpis

    pnls = np.array([t['pnl'] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    total = len(pnls)

    kpis['win_rate'] = float(len(wins) / total * 100)
    gross_profit = float(wins.sum())
    gross_loss = float(abs(losses.sum()))
    if gross_loss > 0:
        kpis['profit_factor'] = gross_profit / gross_loss
    elif gross_profit > 0:
        kpis['profit_factor'] = 9999.99
    kpis['total_pnl'] = float(pnls.sum())

    # Best/worst PnL and R multiples
    kpis['best_win'] = float(wins.max()) if len(wins) else 0.0
    kpis['worst_loss'] = float(losses.min()) if len(losses) else 0.0

    # Extract R multiples; fall back to PnL / intended risk for live/paper trades
    default_risk = 462.27
    r_values = []
    for t in trades:
        raw = t.get('raw', {}) or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        result_r = raw.get('result_r')
        if result_r is None:
            result_r = t['pnl'] / default_risk if default_risk else 0.0
        r_values.append(float(result_r))
    r_values = np.array(r_values, dtype=float)
    kpis['best_r'] = float(r_values.max()) if len(r_values) else 0.0
    kpis['worst_r'] = float(r_values.min()) if len(r_values) else 0.0
    kpis['avg_r'] = float(r_values.mean()) if len(r_values) else 0.0

    kpis['avg_win'] = float(wins.mean()) if len(wins) else 0.0
    kpis['avg_loss'] = float(losses.mean()) if len(losses) else 0.0

    # Max consecutive wins / losses (trades are assumed ordered by exit time)
    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)
    kpis['consecutive_wins'] = int(max_win_streak)
    kpis['consecutive_losses'] = int(max_loss_streak)

    # Points captured per trade (entry -> exit in trade direction).
    # Closed-PnL does not carry stop-loss history, so this is the honest
    # proxy for "points trailed" available exchange-side.
    points_captured = []
    for t in trades:
        if t['qty'] and t['entry_price'] and t['exit_price']:
            direction = 1 if t['side'] == 'Buy' else -1
            points_captured.append((t['exit_price'] - t['entry_price']) * direction)
    if points_captured:
        positive = [p for p in points_captured if p > 0]
        kpis['avg_points_trailed'] = float(np.mean(positive)) if positive else 0.0

    # Equity-based KPIs from the trade-built curve
    if equity_points:
        start = float(equity_points[0][1])
        final = float(equity_points[-1][1])
        kpis['final_equity'] = final
        if start:
            kpis['total_return'] = (final / start - 1.0) * 100

        equity = pd.Series([p[1] for p in equity_points],
                           index=pd.to_datetime([p[0] for p in equity_points], unit='s'))
        running_max = equity.cummax()
        kpis['max_drawdown'] = float(((equity - running_max) / running_max).min() * 100)
        # Static drawdown: from the fixed starting capital (not running peak)
        if start:
            kpis['max_static_drawdown'] = float(((equity - start) / start).min() * 100)
        else:
            kpis['max_static_drawdown'] = 0.0

        monthly = equity.resample('ME').last().dropna()
        if len(monthly) == 1:
            if start:
                monthly_returns = pd.Series([(float(monthly.iloc[-1]) / start - 1.0) * 100])
            else:
                monthly_returns = pd.Series(dtype=float)
        else:
            monthly_returns = monthly.pct_change().dropna() * 100
        if not monthly_returns.empty:
            kpis['avg_monthly_return'] = float(monthly_returns.mean())

        daily = _daily_series(equity_points)
        if len(daily) >= 2:
            daily_returns = daily.pct_change().dropna()
            std = daily_returns.std()
            if std and not np.isnan(std) and std > 0:
                kpis['sharpe_ratio'] = float(daily_returns.mean() / std * np.sqrt(252))

    return kpis
