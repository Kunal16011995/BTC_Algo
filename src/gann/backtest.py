"""
Paper-trading configuration for Strategy 1: Fixed Gann Overnight Chart Method.

Optimized parameters from the robustness grid:
- Daily reversal threshold: 1.5%
- Weekly trend threshold: 1.0%
- Trailing stop: 0.5% beyond the latest higher bottom / lower top
- 3x account leverage, fixed $100k base unit, pyramiding every 4%

This variant had the best risk-adjusted returns in the sensitivity test and
remained robust in the OOS period.
"""

import os
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
INITIAL_CAPITAL = 100_000.0
LEVERAGE = 1.5                 # max account leverage (margin = notional / leverage)
BASE_UNIT_NOTIONAL_PCT = 0.75  # each base entry = 75% of prior-day equity as notional
STOP_LOSS_PCT = 0.5            # stop 0.5% beyond the latest higher bottom / lower top
DAILY_REVERSAL_THRESHOLD_PCT = 1.5
WEEKLY_REVERSAL_THRESHOLD_PCT = 1.0
PYRAMID_STEP_PCT = 4.0
PYRAMID_MAX_LAYERS = 4
TRADING_FEE_PCT = 0.06         # Bybit BTCUSDT perpetual taker fee (per side)
DAILY_FUNDING_PCT = 0.01       # conservative perpetual funding estimate
SLIPPAGE_PCT = 0.03            # slippage on normal market entries/exits (per side)
STOP_SLIPPAGE_PCT = 0.05       # extra slippage on stop-market exits (per side)
COMPOUND_POSITION_SIZE = False   # True = scale with equity; False = fixed $100k base unit
PLOT_RESULTS = True

# Total cost per side for entries and non-stop exits
ENTRY_COST_PCT = TRADING_FEE_PCT + SLIPPAGE_PCT
EXIT_COST_PCT = TRADING_FEE_PCT + SLIPPAGE_PCT
STOP_EXIT_COST_PCT = TRADING_FEE_PCT + STOP_SLIPPAGE_PCT


def _get_base_notional(equity):
    """Return the base unit notional for entries and pyramids."""
    if COMPOUND_POSITION_SIZE:
        return equity * BASE_UNIT_NOTIONAL_PCT
    return INITIAL_CAPITAL * BASE_UNIT_NOTIONAL_PCT


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_data():
    data_path = str(Path(__file__).resolve().parent.parent / 'data' / 'btc_daily_bybit.csv')
    df = pd.read_csv(data_path, parse_dates=['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Weekly trend filter (higher-timeframe regime)
# ---------------------------------------------------------------------------
def compute_weekly_trend(prices_df, threshold_pct):
    n = len(prices_df)
    weekly_trend = np.zeros(n, dtype=int)
    prev_week_low = np.zeros(n)
    prev_week_high = np.zeros(n)
    cur_week_low = np.zeros(n)
    cur_week_high = np.zeros(n)

    for i in range(n):
        week_start = i - prices_df['timestamp'].iloc[i].dayofweek
        if week_start < 0:
            week_start = 0
        cur_week_low[i] = prices_df['low'].iloc[week_start:i+1].min()
        cur_week_high[i] = prices_df['high'].iloc[week_start:i+1].max()

        prior_week_end = week_start - 1
        if prior_week_end < 0:
            prev_week_low[i] = prices_df['low'].iloc[0]
            prev_week_high[i] = prices_df['high'].iloc[0]
        else:
            prior_week_start = prior_week_end - prices_df['timestamp'].iloc[prior_week_end].dayofweek
            if prior_week_start < 0:
                prior_week_start = 0
            prev_week_low[i] = prices_df['low'].iloc[prior_week_start:prior_week_end+1].min()
            prev_week_high[i] = prices_df['high'].iloc[prior_week_start:prior_week_end+1].max()

    for i in range(1, n):
        if weekly_trend[i-1] == 0:
            if cur_week_low[i] > prev_week_low[i]:
                weekly_trend[i] = 1
            elif cur_week_low[i] < prev_week_low[i] * (1 - threshold_pct / 100):
                weekly_trend[i] = -1
            else:
                weekly_trend[i] = 0
        elif weekly_trend[i-1] == 1:
            if cur_week_low[i] < prev_week_low[i] * (1 - threshold_pct / 100):
                weekly_trend[i] = -1
            else:
                weekly_trend[i] = 1
        else:
            if cur_week_high[i] > prev_week_high[i]:
                weekly_trend[i] = 1
            else:
                weekly_trend[i] = -1

    return weekly_trend, prev_week_low, prev_week_high


# ---------------------------------------------------------------------------
# Daily overnight trend using close-based confirmation
# ---------------------------------------------------------------------------
def compute_overnight_trend(df, threshold_pct):
    overnight_trend = np.zeros(len(df), dtype=int)
    for i in range(1, len(df)):
        prev_low = df.loc[i-1, 'low']
        prev_high = df.loc[i-1, 'high']
        today_close = df.loc[i, 'close']
        today_low = df.loc[i, 'low']

        if overnight_trend[i-1] <= 0:
            if today_close > prev_high:
                overnight_trend[i] = 1
            elif today_low < prev_low * (1 - threshold_pct / 100):
                overnight_trend[i] = -1
            else:
                overnight_trend[i] = overnight_trend[i-1]
        else:
            if today_close < prev_low:
                overnight_trend[i] = -1
            elif today_low < prev_low * (1 - threshold_pct / 100):
                overnight_trend[i] = -1
            else:
                overnight_trend[i] = 1

    return overnight_trend


# ---------------------------------------------------------------------------
# Helpers for trailing stop basis
# ---------------------------------------------------------------------------
def latest_higher_bottom(idx, trend_series, low_series):
    if idx <= 0:
        return low_series.iloc[idx]
    latest = low_series.iloc[idx-1]
    for j in range(idx-1, 0, -1):
        if trend_series.iloc[j] != 1:
            break
        if low_series.iloc[j] > low_series.iloc[j-1]:
            latest = low_series.iloc[j]
            break
    return latest


def latest_lower_top(idx, trend_series, high_series):
    if idx <= 0:
        return high_series.iloc[idx]
    latest = high_series.iloc[idx-1]
    for j in range(idx-1, 0, -1):
        if trend_series.iloc[j] != -1:
            break
        if high_series.iloc[j] < high_series.iloc[j-1]:
            latest = high_series.iloc[j]
            break
    return latest


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
class Trade:
    def __init__(self, entry_date, entry_price, direction, size_btc, layer, entry_reason):
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.direction = direction
        self.size_btc = size_btc
        self.layer = layer
        self.entry_reason = entry_reason
        self.exit_date = None
        self.exit_price = None
        self.pnl = 0.0
        self.exit_reason = None


def _margin_required(notional):
    return notional / LEVERAGE


def position_notional(position, price):
    if position is None:
        return 0.0
    return position['total_size'] * price


def close_position(date, exit_price, exit_reason, position, capital):
    realized = 0.0
    exit_cost_pct = STOP_EXIT_COST_PCT if exit_reason == 'stop_loss' else EXIT_COST_PCT
    trades = position['trades']
    for trade in trades:
        entry_notional = trade.size_btc * trade.entry_price
        exit_notional = trade.size_btc * exit_price
        costs = entry_notional * ENTRY_COST_PCT / 100 + exit_notional * exit_cost_pct / 100
        trade_pnl = trade.direction * trade.size_btc * (exit_price - trade.entry_price) - costs
        trade.exit_date = date
        trade.exit_price = exit_price
        trade.pnl = trade_pnl
        trade.exit_reason = exit_reason
        realized += trade_pnl
    return None, capital + realized, trades


def run_backtest(df, direction, plot=False, output_suffix='', use_ema200_filter=False, stop_loss_pct=None,
                 leverage=None, base_unit_notional_pct=None, compound_position_size=None):
    """Run the Gann overnight backtest for a given direction filter.

    Returns a dict with equity_df, trades_df, metrics, and file paths.
    """
    stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else STOP_LOSS_PCT
    leverage = leverage if leverage is not None else LEVERAGE
    base_unit_notional_pct = base_unit_notional_pct if base_unit_notional_pct is not None else BASE_UNIT_NOTIONAL_PCT
    compound_position_size = compound_position_size if compound_position_size is not None else COMPOUND_POSITION_SIZE
    df = df.copy()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    overnight_trend = compute_overnight_trend(df, DAILY_REVERSAL_THRESHOLD_PCT)
    weekly_trend, _, _ = compute_weekly_trend(df, WEEKLY_REVERSAL_THRESHOLD_PCT)

    def _get_base_notional(equity):
        if compound_position_size:
            return equity * base_unit_notional_pct
        return INITIAL_CAPITAL * base_unit_notional_pct

    def _margin_required(notional):
        return notional / leverage

    def ema_filter_allows(trade_dir, price):
        if not use_ema200_filter:
            return True
        ema = df['ema200'].iloc[i]
        if pd.isna(ema):
            return True
        # No longs when price is below 200 EMA; no shorts when price is above 200 EMA.
        if trade_dir == 1 and price < ema:
            return False
        if trade_dir == -1 and price > ema:
            return False
        return True

    capital = INITIAL_CAPITAL
    equity_curve = []
    all_closed_trades = []
    position = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        date = row['timestamp']
        open_p = row['open']
        high_p = row['high']
        low_p = row['low']
        close_p = row['close']

        daily_trend = overnight_trend[i]
        weekly_trend_val = weekly_trend[i]

        prev_unrealized = 0.0
        if position is not None:
            prev_unrealized = position['direction'] * position['total_size'] * (prev_row['close'] - position['avg_price'])
        equity = capital + prev_unrealized

        desired_dir = None
        if weekly_trend_val == 1 and daily_trend == 1 and direction in ('both', 'long_only'):
            desired_dir = 1
        elif weekly_trend_val == -1 and daily_trend == -1 and direction in ('both', 'short_only'):
            desired_dir = -1

        # Apply 200 EMA regime filter to new entries.
        if desired_dir is not None and not ema_filter_allows(desired_dir, close_p):
            desired_dir = None

        if position is not None:
            if position['direction'] == 1:
                new_basis = latest_higher_bottom(i - 1, pd.Series(overnight_trend), df['low'])
                position['stop_basis'] = max(position['stop_basis'], new_basis)
                position['stop_price'] = position['stop_basis'] * (1 - stop_loss_pct / 100)
            else:
                new_basis = latest_lower_top(i - 1, pd.Series(overnight_trend), df['high'])
                position['stop_basis'] = min(position['stop_basis'], new_basis)
                position['stop_price'] = position['stop_basis'] * (1 + stop_loss_pct / 100)

        stop_hit = False
        exit_price = close_p
        if position is not None:
            if position['direction'] == 1 and low_p <= position['stop_price']:
                stop_hit = True
                exit_price = min(open_p, position['stop_price']) if open_p <= position['stop_price'] else position['stop_price']
            elif position['direction'] == -1 and high_p >= position['stop_price']:
                stop_hit = True
                exit_price = max(open_p, position['stop_price']) if open_p >= position['stop_price'] else position['stop_price']

        trend_reversed = position is not None and desired_dir is not None and position['direction'] != desired_dir
        weekly_flipped = position is not None and position['direction'] != weekly_trend_val

        if stop_hit or trend_reversed or weekly_flipped:
            if stop_hit:
                exit_reason = 'stop_loss'
            elif weekly_flipped:
                exit_reason = 'weekly_flip'
                exit_price = close_p
            else:
                exit_reason = 'trend_reversal'
                exit_price = close_p

            position, capital, closed_trades = close_position(date, exit_price, exit_reason, position, capital)
            all_closed_trades.extend(closed_trades)

            new_dir = desired_dir if desired_dir is not None else weekly_trend_val
            # Enforce the long-only / short-only filter on reverse entries too.
            if new_dir == 1 and direction not in ('both', 'long_only'):
                new_dir = None
            if new_dir == -1 and direction not in ('both', 'short_only'):
                new_dir = None
            if new_dir is not None and not ema_filter_allows(new_dir, exit_price):
                new_dir = None

            if new_dir is not None and new_dir == weekly_trend_val:
                multiplier = 2.0 if stop_hit else 1.0
                base_notional = _get_base_notional(equity)
                reverse_notional = base_notional * multiplier
                size_btc = reverse_notional / exit_price
                margin = _margin_required(reverse_notional)

                if margin + reverse_notional * ENTRY_COST_PCT / 100 <= capital:
                    entry_reason = 'stop_reverse_double' if stop_hit else 'trend_reverse'
                    if new_dir == 1:
                        basis = latest_higher_bottom(i - 1, pd.Series(overnight_trend), df['low'])
                        stop_price = basis * (1 - stop_loss_pct / 100)
                    else:
                        basis = latest_lower_top(i - 1, pd.Series(overnight_trend), df['high'])
                        stop_price = basis * (1 + stop_loss_pct / 100)

                    position = {
                        'direction': new_dir,
                        'trades': [Trade(date, exit_price, new_dir, size_btc, 0, entry_reason)],
                        'total_size': size_btc,
                        'avg_price': exit_price,
                        'stop_basis': basis,
                        'stop_price': stop_price,
                        'pyramid_layers': 0,
                        'last_pyramid_price': exit_price,
                    }
            continue

        if position is None and desired_dir is not None:
            base_notional = _get_base_notional(equity)
            size_btc = base_notional / close_p
            margin = _margin_required(base_notional)

            if margin + base_notional * ENTRY_COST_PCT / 100 <= capital:
                if desired_dir == 1:
                    basis = latest_higher_bottom(i - 1, pd.Series(overnight_trend), df['low'])
                    stop_price = basis * (1 - stop_loss_pct / 100)
                else:
                    basis = latest_lower_top(i - 1, pd.Series(overnight_trend), df['high'])
                    stop_price = basis * (1 + stop_loss_pct / 100)

                position = {
                    'direction': desired_dir,
                    'trades': [Trade(date, close_p, desired_dir, size_btc, 0, 'trend_entry')],
                    'total_size': size_btc,
                    'avg_price': close_p,
                    'stop_basis': basis,
                    'stop_price': stop_price,
                    'pyramid_layers': 0,
                    'last_pyramid_price': close_p,
                }
            continue

        if position is not None and position['pyramid_layers'] < PYRAMID_MAX_LAYERS and position['direction'] == weekly_trend_val:
            price_move_pct = (close_p - position['last_pyramid_price']) / position['last_pyramid_price'] * 100
            if position['direction'] == 1 and price_move_pct >= PYRAMID_STEP_PCT:
                add_notional = (_get_base_notional(equity)) * (0.5 ** (position['pyramid_layers'] + 1))
                add_size = add_notional / close_p
                margin = _margin_required(add_notional)
                if margin + add_notional * ENTRY_COST_PCT / 100 <= capital:
                    new_trade = Trade(date, close_p, 1, add_size, position['pyramid_layers'] + 1, 'pyramid')
                    total_cost = position['avg_price'] * position['total_size'] + close_p * add_size
                    position['total_size'] += add_size
                    position['avg_price'] = total_cost / position['total_size']
                    position['trades'].append(new_trade)
                    position['pyramid_layers'] += 1
                    position['last_pyramid_price'] = close_p
            elif position['direction'] == -1 and price_move_pct <= -PYRAMID_STEP_PCT:
                add_notional = (_get_base_notional(equity)) * (0.5 ** (position['pyramid_layers'] + 1))
                add_size = add_notional / close_p
                margin = _margin_required(add_notional)
                if margin + add_notional * ENTRY_COST_PCT / 100 <= capital:
                    new_trade = Trade(date, close_p, -1, add_size, position['pyramid_layers'] + 1, 'pyramid')
                    total_cost = position['avg_price'] * position['total_size'] + close_p * add_size
                    position['total_size'] += add_size
                    position['avg_price'] = total_cost / position['total_size']
                    position['trades'].append(new_trade)
                    position['pyramid_layers'] += 1
                    position['last_pyramid_price'] = close_p

        if position is not None:
            notional = position_notional(position, close_p)
            if direction == 'both':
                funding_cost = position['direction'] * notional * DAILY_FUNDING_PCT / 100
            elif direction == 'long_only':
                funding_cost = notional * DAILY_FUNDING_PCT / 100
            else:
                funding_cost = -notional * DAILY_FUNDING_PCT / 100
            capital -= funding_cost

        unrealized = 0.0
        if position is not None:
            unrealized = position['direction'] * position['total_size'] * (close_p - position['avg_price'])
        equity_curve.append({
            'date': date,
            'equity': capital + unrealized,
            'capital': capital,
            'close': close_p,
            'weekly_trend': weekly_trend_val,
            'daily_trend': daily_trend,
            'position_dir': position['direction'] if position else 0,
            'position_size': position['total_size'] if position else 0,
        })

    if position is not None:
        last_row = df.iloc[-1]
        exit_price = last_row['close']
        date = last_row['timestamp']
        position, capital, closed_trades = close_position(date, exit_price, 'end_of_data', position, capital)
        all_closed_trades.extend(closed_trades)

    final_equity = capital
    equity_df = pd.DataFrame(equity_curve)

    trades_df = pd.DataFrame([{
        'entry_date': t.entry_date,
        'exit_date': t.exit_date,
        'direction': 'Long' if t.direction == 1 else 'Short',
        'entry_price': t.entry_price,
        'exit_price': t.exit_price,
        'size_btc': t.size_btc,
        'layer': t.layer,
        'pnl_usd': t.pnl,
        'entry_reason': t.entry_reason,
        'exit_reason': t.exit_reason,
    } for t in all_closed_trades])

    if not trades_df.empty:
        total_pnl = trades_df['pnl_usd'].sum()
        winning_trades = trades_df[trades_df['pnl_usd'] > 0]
        losing_trades = trades_df[trades_df['pnl_usd'] <= 0]
        win_rate = len(winning_trades) / len(trades_df) * 100
        avg_win = winning_trades['pnl_usd'].mean() if len(winning_trades) > 0 else 0
        avg_loss = losing_trades['pnl_usd'].mean() if len(losing_trades) > 0 else 0
        profit_factor = abs(winning_trades['pnl_usd'].sum() / losing_trades['pnl_usd'].sum()) if len(losing_trades) > 0 and losing_trades['pnl_usd'].sum() != 0 else np.inf

        equity_df['peak'] = equity_df['equity'].cummax()
        equity_df['drawdown'] = (equity_df['equity'] - equity_df['peak']) / equity_df['peak'] * 100
        max_dd = equity_df['drawdown'].min()

        start_date = equity_df['date'].iloc[0]
        end_date = equity_df['date'].iloc[-1]
        years = (end_date - start_date).days / 365.25
        total_return_pct = (final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        cagr = ((final_equity / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0

        daily_returns = equity_df['equity'].pct_change().dropna()
        sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(365)
    else:
        total_pnl = 0
        win_rate = 0
        avg_win = 0
        avg_loss = 0
        profit_factor = 0
        max_dd = 0
        total_return_pct = 0
        cagr = 0
        sharpe = 0

    base_path = str(Path(__file__).resolve().parent.parent)
    suffix = output_suffix or f'_{direction}'
    trades_output = os.path.join(base_path, f'gann_overnight_paper_trades{suffix}.csv')
    equity_output = os.path.join(base_path, f'gann_overnight_paper_equity{suffix}.csv')
    plot_output = os.path.join(base_path, f'gann_overnight_paper_backtest{suffix}.png')

    trades_df.to_csv(trades_output, index=False)
    equity_df.to_csv(equity_output, index=False)

    if plot and not equity_df.empty:
        fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True,
                                 gridspec_kw={'height_ratios': [3, 1, 1]})

        ax1 = axes[0]
        ax1.plot(equity_df['date'], equity_df['close'], label='BTC Close', color='gray', alpha=0.6)
        ax1_twin = ax1.twinx()
        ax1_twin.plot(equity_df['date'], equity_df['equity'], label='Strategy Equity', color='blue', linewidth=1.5)
        ax1.set_ylabel('BTC Price ($)', color='gray')
        ax1_twin.set_ylabel('Equity ($)', color='blue')
        ax1.set_title(f'Paper-Trading Config - Gann Overnight Strategy ({direction}) - BTCUSDT')
        ax1.legend(loc='upper left')
        ax1_twin.legend(loc='upper right')

        ax2 = axes[1]
        ax2.fill_between(equity_df['date'], equity_df['drawdown'], 0, color='red', alpha=0.3)
        ax2.plot(equity_df['date'], equity_df['drawdown'], color='red', linewidth=0.8)
        ax2.set_ylabel('Drawdown (%)')
        ax2.axhline(0, color='black', linewidth=0.5)

        ax3 = axes[2]
        ax3.plot(equity_df['date'], equity_df['position_dir'], drawstyle='steps-post', color='green')
        ax3.set_ylabel('Position (1=Long, -1=Short)')
        ax3.set_xlabel('Date')
        ax3.axhline(0, color='black', linewidth=0.5)

        plt.tight_layout()
        plt.savefig(plot_output, dpi=150)
        plt.close()

    metrics = {
        'direction': direction,
        'final_equity': final_equity,
        'total_return_pct': total_return_pct,
        'cagr': cagr,
        'max_dd': max_dd,
        'total_trades': len(trades_df),
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'sharpe': sharpe,
        'total_pnl': total_pnl,
    }

    return {
        'metrics': metrics,
        'equity_df': equity_df,
        'trades_df': trades_df,
        'trades_output': trades_output,
        'equity_output': equity_output,
        'plot_output': plot_output if (plot and not equity_df.empty) else None,
    }


def print_metrics(metrics):
    print(f'  Direction:        {metrics["direction"]}')
    print(f'  Final equity:     ${metrics["final_equity"]:,.2f}')
    print(f'  Total return:     {metrics["total_return_pct"]:+.2f}%')
    print(f'  CAGR:             {metrics["cagr"]:+.2f}%')
    print(f'  Max drawdown:     {metrics["max_dd"]:.2f}%')
    print(f'  Total trades:     {metrics["total_trades"]}')
    print(f'  Win rate:         {metrics["win_rate"]:.2f}%')
    print(f'  Avg win:          ${metrics["avg_win"]:,.2f}')
    print(f'  Avg loss:         ${metrics["avg_loss"]:,.2f}')
    print(f'  Profit factor:    {metrics["profit_factor"]:.2f}')
    print(f'  Sharpe ratio:     {metrics["sharpe"]:.2f}')
    print(f'  Total PnL:        ${metrics["total_pnl"]:,.2f}')


def analyze_worst_trades(df, trades_df, n=20):
    """Print the worst losing trades and how many would be blocked by a 200 EMA filter."""
    df = df.copy()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    ema_map = pd.Series(df['ema200'].values, index=df['timestamp']).to_dict()

    trades = trades_df.copy()
    trades['ema200_at_entry'] = trades['entry_date'].map(ema_map)
    trades['price_vs_ema'] = trades['entry_price'] - trades['ema200_at_entry']
    trades['blocked_by_ema'] = (
        ((trades['direction'] == 'Long') & (trades['entry_price'] < trades['ema200_at_entry'])) |
        ((trades['direction'] == 'Short') & (trades['entry_price'] > trades['ema200_at_entry']))
    )

    worst = trades.nsmallest(n, 'pnl_usd')
    blocked_count = int(trades['blocked_by_ema'].sum())
    blocked_pnl = float(trades.loc[trades['blocked_by_ema'], 'pnl_usd'].sum())

    print()
    print(f'WORST {n} TRADES (baseline both-direction)')
    print('=' * 100)
    print(f'{"Entry":12} {"Dir":6} {"Entry":>10} {"EMA200":>10} {"Exit":>10} {"PnL":>12} {"Blocked?"}')
    for _, t in worst.iterrows():
        print(
            f'{t["entry_date"].strftime("%Y-%m-%d"):12} {t["direction"]:6} '
            f'{t["entry_price"]:10.2f} {t["ema200_at_entry"]:10.2f} '
            f'{t["exit_price"]:10.2f} {t["pnl_usd"]:12.2f} {"YES" if t["blocked_by_ema"] else "no"}'
        )
    print('=' * 100)
    print(f'Total trades that would be blocked by 200 EMA filter: {blocked_count} / {len(trades)}')
    print(f'PnL of those blocked trades: ${blocked_pnl:,.2f}')
    print()
    return trades


if __name__ == '__main__':
    df = load_data()
    print('=' * 60)
    print('WD GANN OVERNIGHT STRATEGY - DIRECTIONAL COMPARISON')
    print('=' * 60)
    print(f'Data range: {df["timestamp"].min()} to {df["timestamp"].max()}')
    print(f'Initial capital: ${INITIAL_CAPITAL:,.2f}')
    print()

    results = []
    for direction in ('both', 'long_only', 'short_only'):
        result = run_backtest(df, direction=direction, plot=PLOT_RESULTS, output_suffix='')
        results.append(result)
        print_metrics(result['metrics'])
        print(f'  Trades CSV:       {result["trades_output"]}')
        print(f'  Equity CSV:       {result["equity_output"]}')
        print(f'  Plot PNG:         {result["plot_output"]}')
        print('-' * 60)

    # The default dashboard tab expects the unsuffixed 'both' files.
    # Rename the 'both' outputs to the canonical names if they were suffixed.
    base_path = str(Path(__file__).resolve().parent.parent)
    for suffix in ('_both', '_long_only', '_short_only'):
        for prefix in ('gann_overnight_paper_trades', 'gann_overnight_paper_equity', 'gann_overnight_paper_backtest'):
            src = os.path.join(base_path, f'{prefix}{suffix}.csv' if 'trades' in prefix or 'equity' in prefix else f'{prefix}{suffix}.png')
            dst = os.path.join(base_path, f'{prefix}.csv' if 'trades' in prefix else f'{prefix}.csv' if 'equity' in prefix else f'{prefix}.png')
            if suffix == '_both' and os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
            elif suffix != '_both' and os.path.exists(src):
                pass  # keep side files

    print()
    print('SUMMARY COMPARISON TABLE')
    print('=' * 60)
    summary = pd.DataFrame([r['metrics'] for r in results])
    summary = summary[['direction', 'total_return_pct', 'cagr', 'max_dd', 'total_trades', 'win_rate', 'profit_factor', 'sharpe']]
    print(summary.to_string(index=False))
    print('=' * 60)

    # -----------------------------------------------------------------------
    # 200 EMA regime filter test
    # -----------------------------------------------------------------------
    print()
    print('200 EMA REGIME FILTER TEST')
    print('=' * 60)
    baseline = run_backtest(df, direction='both', plot=False, output_suffix='_baseline_for_filter', use_ema200_filter=False)
    filtered = run_backtest(df, direction='both', plot=PLOT_RESULTS, output_suffix='_filtered', use_ema200_filter=True)

    print('Baseline (no filter):')
    print_metrics(baseline['metrics'])
    print()
    print('With 200 EMA filter:')
    print_metrics(filtered['metrics'])

    print()
    print('FILTER COMPARISON TABLE')
    print('=' * 60)
    comp = pd.DataFrame([baseline['metrics'], filtered['metrics']])
    comp = comp[['direction', 'total_return_pct', 'cagr', 'max_dd', 'total_trades', 'win_rate', 'profit_factor', 'sharpe']]
    print(comp.to_string(index=False))
    print('=' * 60)

    # Clean up temporary baseline-for-filter files.
    for prefix in ('gann_overnight_paper_trades', 'gann_overnight_paper_equity', 'gann_overnight_paper_backtest'):
        for ext in ('csv', 'png'):
            temp = os.path.join(base_path, f'{prefix}_baseline_for_filter.{ext}')
            if os.path.exists(temp):
                os.remove(temp)

    analyze_worst_trades(df, baseline['trades_df'], n=20)

    # -----------------------------------------------------------------------
    # Trailing-stop parameter optimisation
    # -----------------------------------------------------------------------
    print()
    print('TRAILING STOP PARAMETER OPTIMISATION')
    print('=' * 60)
    stop_grid = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
    stop_results = []
    for stop_pct in stop_grid:
        result = run_backtest(df, direction='both', plot=False, output_suffix=f'_stop{stop_pct}', use_ema200_filter=False, stop_loss_pct=stop_pct)
        metrics = result['metrics'].copy()
        metrics['stop_loss_pct'] = stop_pct
        stop_results.append(metrics)
        print(f'  stop {stop_pct:>4.2f}% -> ROI {metrics["total_return_pct"]:+8.2f}% | DD {metrics["max_dd"]:7.2f}% | Trades {metrics["total_trades"]:4d} | Sharpe {metrics["sharpe"]:.2f}')

    stop_summary = pd.DataFrame(stop_results)
    stop_summary = stop_summary[['stop_loss_pct', 'total_return_pct', 'cagr', 'max_dd', 'total_trades', 'win_rate', 'profit_factor', 'sharpe']]
    stop_summary_path = os.path.join(base_path, 'gann_overnight_stop_optimisation.csv')
    stop_summary.to_csv(stop_summary_path, index=False)
    print()
    print('STOP OPTIMISATION TABLE')
    print('=' * 60)
    print(stop_summary.to_string(index=False))
    print('=' * 60)
    print(f'Saved summary to: {stop_summary_path}')

    # -----------------------------------------------------------------------
    # Position-size / leverage sweep for drawdown reduction
    # -----------------------------------------------------------------------
    print()
    print('POSITION SIZE / LEVERAGE SWEEP')
    print('=' * 60)
    leverage_grid = [1.0, 1.5, 2.0, 2.5, 3.0]
    base_unit_grid = [0.5, 0.75, 1.0]
    sizing_results = []
    for lev in leverage_grid:
        for bu in base_unit_grid:
            result = run_backtest(
                df,
                direction='both',
                plot=False,
                output_suffix=f'_lev{lev}_bu{bu}',
                use_ema200_filter=False,
                stop_loss_pct=STOP_LOSS_PCT,
                leverage=lev,
                base_unit_notional_pct=bu,
                compound_position_size=False,
            )
            metrics = result['metrics'].copy()
            metrics['leverage'] = lev
            metrics['base_unit_notional_pct'] = bu
            metrics['calmar'] = metrics['cagr'] / abs(metrics['max_dd']) if metrics['max_dd'] != 0 else 0
            sizing_results.append(metrics)
            print(
                f'  lev {lev:.1f}x | base {bu*100:5.1f}% '
                f'-> ROI {metrics["total_return_pct"]:+8.2f}% | DD {metrics["max_dd"]:7.2f}% | '
                f'Trades {metrics["total_trades"]:4d} | Sharpe {metrics["sharpe"]:.2f} | Calmar {metrics["calmar"]:.2f}'
            )

    sizing_summary = pd.DataFrame(sizing_results)
    sizing_summary = sizing_summary[
        ['leverage', 'base_unit_notional_pct', 'total_return_pct', 'cagr', 'max_dd', 'total_trades', 'win_rate', 'profit_factor', 'sharpe', 'calmar']
    ]
    sizing_summary_path = os.path.join(base_path, 'gann_overnight_sizing_optimisation.csv')
    sizing_summary.to_csv(sizing_summary_path, index=False)
    print()
    print('SIZING OPTIMISATION TABLE')
    print('=' * 60)
    print(sizing_summary.to_string(index=False))
    print('=' * 60)
    print(f'Saved summary to: {sizing_summary_path}')

    # Clean up individual run files from the sweep.
    for f in os.listdir(base_path):
        if f.startswith('gann_overnight_paper_') and '_lev' in f:
            os.remove(os.path.join(base_path, f))
