"""
Reusable Gann overnight strategy engine.

This module exposes the signal/logic used by both the backtest
(gann_overnight_paper.py) and the live forward-test bot
(gann_forward_bot.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


DIRECTION_LONG = 1
DIRECTION_SHORT = -1


def body_ratio(open_, high, low, close):
    body = abs(close - open_)
    range_ = high - low
    return body / range_ if range_ > 0 else 0.0


def compute_weekly_trend(prices_df, threshold_pct=1.0):
    """Return an integer series: 1=bullish, -1=bearish, 0=neutral."""
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


def compute_overnight_trend(df, threshold_pct=1.5):
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


def prepare_dataframe(df):
    """Sort, reset index, and add derived trend columns."""
    df = df.sort_values('timestamp').reset_index(drop=True)
    weekly_trend, _, _ = compute_weekly_trend(df, threshold_pct=1.0)
    overnight_trend = compute_overnight_trend(df, threshold_pct=1.5)
    df['weekly_trend'] = weekly_trend
    df['overnight_trend'] = overnight_trend
    return df


def signal_for_day(df, idx, direction='both'):
    """Return the desired direction and stop basis for a given daily index.

    Returns (desired_dir, stop_basis) where desired_dir is 1, -1, or None.
    """
    weekly_trend_val = df.loc[idx, 'weekly_trend']
    daily_trend = df.loc[idx, 'overnight_trend']
    close_p = df.loc[idx, 'close']

    desired_dir = None
    if weekly_trend_val == 1 and daily_trend == 1 and direction in ('both', 'long_only'):
        desired_dir = 1
    elif weekly_trend_val == -1 and daily_trend == -1 and direction in ('both', 'short_only'):
        desired_dir = -1

    if desired_dir == 1:
        basis = latest_higher_bottom(idx - 1, df['overnight_trend'], df['low'])
    elif desired_dir == -1:
        basis = latest_lower_top(idx - 1, df['overnight_trend'], df['high'])
    else:
        basis = None

    return desired_dir, basis


def stop_price_for_direction(direction, basis, stop_loss_pct=0.5):
    if direction == 1:
        return basis * (1 - stop_loss_pct / 100)
    if direction == -1:
        return basis * (1 + stop_loss_pct / 100)
    return None
