"""
Backtest engine for SSRN-6725880 FVG strategy on BTCUSDT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from strategy import Direction, Grade, Setup, generate_setups


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: Literal["long", "short"]
    grade: str
    entry_price: float
    exit_price: float
    size_usd: float
    gross_pnl: float
    cost: float
    net_pnl: float
    result_r: float
    exit_reason: str


def simulate_trade(
    setup: Setup,
    df5: pd.DataFrame,
    start_idx: int,
    size_usd: float,
    cost_pct: float = 0.0,
    cost_per_trade: float = 0.0,
    entry_cost_pct: float | None = None,
    exit_cost_pct: float | None = None,
) -> Trade | None:
    """Simulate a single setup from entry to exit using 5m data."""
    if start_idx >= len(df5) - 1:
        return None

    if entry_cost_pct is None:
        entry_cost_pct = cost_pct
    if exit_cost_pct is None:
        exit_cost_pct = cost_pct

    direction = setup.direction
    entry_price = setup.entry_price
    stop_price = setup.stop_price
    tp1_price = setup.tp1_price
    tp2_price = setup.tp2_price
    runner_max = setup.runner_max_price
    trail_dist = abs(entry_price - tp1_price)  # 1R distance

    # Cost on entry
    entry_cost = size_usd * entry_cost_pct + cost_per_trade / 2

    # State
    tp1_hit = False
    tp2_hit = False
    trailing_stop = None
    extreme_price = entry_price

    for i in range(start_idx + 1, len(df5)):
        row = df5.iloc[i]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]

        # Update extreme price for trailing stop
        if direction == Direction.LONG:
            extreme_price = max(extreme_price, h)
        else:
            extreme_price = min(extreme_price, l)

        # Stop loss check
        if direction == Direction.LONG:
            if l <= stop_price:
                exit_price = min(o, stop_price) if c >= stop_price else stop_price
                exit_reason = "stop_loss"
                break
        else:
            if h >= stop_price:
                exit_price = max(o, stop_price) if c <= stop_price else stop_price
                exit_reason = "stop_loss"
                break

        # TP1 check and activate trailing stop
        if not tp1_hit:
            hit = (direction == Direction.LONG and h >= tp1_price) or (
                direction == Direction.SHORT and l <= tp1_price
            )
            if hit:
                tp1_hit = True
                if direction == Direction.LONG:
                    trailing_stop = max(tp1_price, extreme_price - trail_dist)
                else:
                    trailing_stop = min(tp1_price, extreme_price + trail_dist)

        # Update trailing stop
        if tp1_hit:
            if direction == Direction.LONG:
                trailing_stop = max(trailing_stop, extreme_price - trail_dist)
            else:
                trailing_stop = min(trailing_stop, extreme_price + trail_dist)

        # TP2 check
        if not tp2_hit:
            hit_tp2 = (direction == Direction.LONG and h >= tp2_price) or (
                direction == Direction.SHORT and l <= tp2_price
            )
            if hit_tp2:
                tp2_hit = True

        # Runner max cap (hard take-profit for the runner portion)
        hit_runner = (direction == Direction.LONG and h >= runner_max) or (
            direction == Direction.SHORT and l <= runner_max
        )
        if hit_runner:
            exit_price = runner_max
            exit_reason = "runner_max"
            break

        # Trailing stop hit (only after TP1 has been reached)
        if tp1_hit:
            if direction == Direction.LONG and l <= trailing_stop:
                exit_price = min(o, trailing_stop) if c >= trailing_stop else trailing_stop
                exit_reason = "trail_1r"
                break
            if direction == Direction.SHORT and h >= trailing_stop:
                exit_price = max(o, trailing_stop) if c <= trailing_stop else trailing_stop
                exit_reason = "trail_1r"
                break

        # End of data
        if i == len(df5) - 1:
            exit_price = c
            exit_reason = "end_of_data"
            break

    # Compute P&L
    if direction == Direction.LONG:
        gross_pnl = size_usd * (exit_price - entry_price) / entry_price
    else:
        gross_pnl = size_usd * (entry_price - exit_price) / entry_price

    exit_cost = size_usd * exit_cost_pct + cost_per_trade / 2
    total_cost = entry_cost + exit_cost
    net_pnl = gross_pnl - total_cost

    risk_amount = size_usd * abs(entry_price - stop_price) / entry_price
    result_r = net_pnl / risk_amount if risk_amount > 0 else 0.0

    return Trade(
        entry_time=setup.timestamp,
        exit_time=df5["timestamp"].iloc[i],
        direction="long" if direction == Direction.LONG else "short",
        grade=setup.grade.value,
        entry_price=entry_price,
        exit_price=exit_price,
        size_usd=size_usd,
        gross_pnl=gross_pnl,
        cost=total_cost,
        net_pnl=net_pnl,
        result_r=result_r,
        exit_reason=exit_reason,
    )


def run_backtest(
    df5: pd.DataFrame,
    df1h: pd.DataFrame,
    capital: float = 10_000.0,
    risk_per_trade_pct: float | None = 0.01,
    fixed_risk_amount: float | None = None,
    daily_loss_cap_pct: float | None = 0.003,
    fixed_daily_loss_cap: float | None = None,
    cost_pct: float = 0.0,
    cost_per_trade: float = 0.0,
    entry_cost_pct: float | None = None,
    exit_cost_pct: float | None = None,
    direction_filter=None,
    **strategy_kwargs,
) -> dict:
    """Run full backtest and return equity curve + trade list + metrics.

    Provide either `risk_per_trade_pct` (percentage of current equity) or
    `fixed_risk_amount` (fixed dollar amount, paper style). Same for daily cap.
    Use `entry_cost_pct` / `exit_cost_pct` for asymmetric cost assumptions.
    """
    setups = generate_setups(df5, df1h, direction_filter=direction_filter, **strategy_kwargs)

    equity = [capital]
    equity_time = [df5["timestamp"].iloc[0]]
    trades: list[Trade] = []
    daily_pnl: dict[pd.Timestamp, float] = {}

    for setup in setups:
        # Find 5m index for setup timestamp
        mask = df5["timestamp"] == setup.timestamp
        if not mask.any():
            continue
        start_idx = int(mask.idxmax())

        # Daily loss cap check
        day = setup.timestamp.normalize()
        day_pnl_so_far = daily_pnl.get(day, 0.0)
        daily_cap = fixed_daily_loss_cap if fixed_daily_loss_cap is not None else (capital * daily_loss_cap_pct if daily_loss_cap_pct is not None else float("inf"))
        if day_pnl_so_far <= -daily_cap:
            continue

        # Position sizing
        if fixed_risk_amount is not None:
            risk_amount = fixed_risk_amount
        elif risk_per_trade_pct is not None:
            risk_amount = equity[-1] * risk_per_trade_pct
        else:
            risk_amount = capital * 0.01  # fallback
        stop_distance = abs(setup.entry_price - setup.stop_price) / setup.entry_price
        size_usd = risk_amount / stop_distance

        trade = simulate_trade(
            setup, df5, start_idx, size_usd, cost_pct, cost_per_trade,
            entry_cost_pct=entry_cost_pct, exit_cost_pct=exit_cost_pct,
        )
        if trade is None:
            continue

        trades.append(trade)

        # Update equity
        new_equity = equity[-1] + trade.net_pnl
        equity.append(new_equity)
        equity_time.append(trade.exit_time)

        # Update daily P&L
        day = trade.exit_time.normalize()
        daily_pnl[day] = daily_pnl.get(day, 0.0) + trade.net_pnl

    equity_df = pd.DataFrame({"timestamp": equity_time, "equity": equity})
    trades_df = pd.DataFrame([t.__dict__ for t in trades])

    return {
        "equity": equity_df,
        "trades": trades_df,
        "setups": setups,
    }


def compute_metrics(equity_df: pd.DataFrame, trades_df: pd.DataFrame, capital: float) -> dict:
    """Compute standard backtest metrics."""
    if trades_df.empty:
        return {"error": "No trades"}

    eq = equity_df.set_index("timestamp")["equity"]
    returns = eq.resample("D").last().dropna().pct_change().dropna()

    total_return = eq.iloc[-1] / capital - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / capital) ** (1 / years) - 1 if years > 0 else 0.0
    ann_vol = returns.std() * np.sqrt(365)
    sharpe = (returns.mean() * 365) / (returns.std() * np.sqrt(365)) if returns.std() > 0 else 0.0

    # Drawdown
    peak = eq.cummax()
    dd = (eq - peak) / peak
    max_dd = dd.min()

    # Trade metrics
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    win_rate = len(wins) / len(trades_df)
    profit_factor = wins["net_pnl"].sum() / abs(losses["net_pnl"].sum()) if len(losses) > 0 else np.inf
    avg_win = wins["net_pnl"].mean() if len(wins) > 0 else 0.0
    avg_loss = losses["net_pnl"].mean() if len(losses) > 0 else 0.0
    payoff_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else 0.0

    return {
        "start_capital": capital,
        "final_equity": eq.iloc[-1],
        "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100,
        "ann_vol_pct": ann_vol * 100,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd * 100,
        "num_trades": len(trades_df),
        "win_rate_pct": win_rate * 100,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "avg_trade_pnl": trades_df["net_pnl"].mean(),
        "avg_r": trades_df["result_r"].mean(),
        "total_cost": trades_df["cost"].sum(),
    }
