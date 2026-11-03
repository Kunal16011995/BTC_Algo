"""
SSRN-6725880: A/A+ Momentum Fair Value Gap strategy adapted for BTCUSDT.

All percentage distances are expressed as decimals (e.g. 0.003 = 0.30%).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import numpy as np
import pandas as pd


class Direction(Enum):
    LONG = 1
    SHORT = -1


class Grade(Enum):
    A_PLUS = "A+"
    A = "A"


@dataclass(frozen=True)
class Setup:
    timestamp: pd.Timestamp
    direction: Direction
    grade: Grade
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    runner_max_price: float
    htf_aligned: bool
    momentum_leg_idx: int


def body_ratio(open_: float, high: float, low: float, close: float) -> float:
    body = abs(close - open_)
    range_ = high - low
    return body / range_ if range_ > 0 else 0.0


def detect_momentum_legs(df5: pd.DataFrame, min_body_ratio: float = 0.52) -> list[dict]:
    """Return list of momentum legs on the 5m timeframe.

    A leg is a sequence of >= 2 consecutive aligned 5m candles whose body ratio
    is >= min_body_ratio, ending with a close that breaches the swing high/low
    of the prior 10 candles.
    """
    n = len(df5)
    legs: list[dict] = []
    i = 0
    while i < n - 1:
        # Check if candle i starts a potential leg
        br = body_ratio(df5["open"].iloc[i], df5["high"].iloc[i], df5["low"].iloc[i], df5["close"].iloc[i])
        if br < min_body_ratio:
            i += 1
            continue

        direction = Direction.LONG if df5["close"].iloc[i] >= df5["open"].iloc[i] else Direction.SHORT
        start = i
        j = i
        min_br_in_leg = br

        # Extend while candles are aligned and meet minimum body ratio
        while j < n:
            br_j = body_ratio(
                df5["open"].iloc[j], df5["high"].iloc[j], df5["low"].iloc[j], df5["close"].iloc[j]
            )
            dir_j = Direction.LONG if df5["close"].iloc[j] >= df5["open"].iloc[j] else Direction.SHORT
            if dir_j != direction or br_j < min_body_ratio:
                break
            min_br_in_leg = min(min_br_in_leg, br_j)
            j += 1

        leg_len = j - start
        if leg_len >= 2:
            end_idx = j - 1
            # Swing breach: final close must exceed the max/min of prior 10 candles
            lookback_start = max(0, start - 10)
            if direction == Direction.LONG:
                swing_high = df5["high"].iloc[lookback_start:start].max()
                if df5["close"].iloc[end_idx] > swing_high:
                    legs.append(
                        {
                            "start_idx": start,
                            "end_idx": end_idx,
                            "direction": direction,
                            "min_body_ratio": min_br_in_leg,
                        }
                    )
                    i = j
                    continue
            else:
                swing_low = df5["low"].iloc[lookback_start:start].min()
                if df5["close"].iloc[end_idx] < swing_low:
                    legs.append(
                        {
                            "start_idx": start,
                            "end_idx": end_idx,
                            "direction": direction,
                            "min_body_ratio": min_br_in_leg,
                        }
                    )
                    i = j
                    continue
        i += 1

    return legs


def find_fvg(df5: pd.DataFrame, start_idx: int, max_lookforward: int = 24) -> dict | None:
    """Find the first bullish/bearish FVG in the 24 candles after start_idx.

    Bullish FVG: High[i-2] < Low[i]
    Bearish FVG: Low[i-2] > High[i]
    Returns the FVG midpoint and the index where it completes.
    """
    n = len(df5)
    end = min(n - 1, start_idx + max_lookforward)

    for i in range(start_idx + 3, end + 1):
        h_i2 = df5["high"].iloc[i - 2]
        l_i2 = df5["low"].iloc[i - 2]
        h_i = df5["high"].iloc[i]
        l_i = df5["low"].iloc[i]

        if h_i2 < l_i:  # bullish FVG
            midpoint = (h_i2 + l_i) / 2.0
            return {"idx": i, "direction": Direction.LONG, "midpoint": midpoint}
        if l_i2 > h_i:  # bearish FVG
            midpoint = (l_i2 + h_i) / 2.0
            return {"idx": i, "direction": Direction.SHORT, "midpoint": midpoint}

    return None


def compute_htf_levels(df1h: pd.DataFrame) -> pd.DataFrame:
    """Add EMA-20, EMA-50 and 30-period POC to the 1h dataframe."""
    df = df1h.copy()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    def rolling_poc(close: pd.Series, high: pd.Series, low: pd.Series, window: int = 30) -> pd.Series:
        pocs = []
        for i in range(len(close)):
            if i < window - 1:
                pocs.append(np.nan)
                continue
            sub_high = high.iloc[i - window + 1 : i + 1]
            sub_low = low.iloc[i - window + 1 : i + 1]
            hist, bin_edges = np.histogram(
                np.concatenate([sub_high.values, sub_low.values]), bins=30
            )
            max_bin = np.argmax(hist)
            poc = (bin_edges[max_bin] + bin_edges[max_bin + 1]) / 2.0
            pocs.append(poc)
        return pd.Series(pocs, index=close.index)

    df["poc30"] = rolling_poc(df["close"], df["high"], df["low"], window=30)
    return df


def build_htf_map(df1h: pd.DataFrame, df5: pd.DataFrame) -> pd.DataFrame:
    """Map each 5m candle to the most recent completed 1h candle's HTF levels."""
    df1h = df1h.set_index("timestamp")
    df5 = df5.copy()
    df5["htf_time"] = df5["timestamp"].dt.floor("h") - pd.Timedelta(hours=1)
    # Align to the nearest 1h timestamp present in df1h
    df5 = df5.merge(
        df1h[["ema20", "ema50", "poc30"]].rename(
            columns={"ema20": "htf_ema20", "ema50": "htf_ema50", "poc30": "htf_poc30"}
        ),
        left_on="htf_time",
        right_index=True,
        how="left",
    )
    return df5


def htf_confluence(entry_price: float, htf_levels: pd.Series, proximity_pct: float) -> bool:
    """True if entry_price is within proximity_pct of any HTF level."""
    for level in [htf_levels["htf_ema20"], htf_levels["htf_ema50"], htf_levels["htf_poc30"]]:
        if pd.isna(level):
            continue
        if abs(entry_price - level) / entry_price <= proximity_pct:
            return True
    return False


def session_label(ts: pd.Timestamp, include_asia: bool = True) -> str | None:
    """Return session label or None if outside allowed sessions.

    Default windows:
        Asia:     00:00-08:00 UTC
        London:   08:00-11:00 UTC
        Overlap:  13:00-15:00 UTC
        New York: 13:00-16:00 UTC
    """
    t = ts.time()
    hour = t.hour
    minute = t.minute
    if include_asia:
        if (0 <= hour < 8) or (hour == 8 and minute == 0):
            return "Asia"
    if 8 <= hour < 11 or (hour == 11 and minute == 0):
        return "London"
    if 13 <= hour < 15 or (hour == 15 and minute == 0):
        return "Overlap"
    if 13 <= hour < 16 or (hour == 16 and minute == 0):
        return "New York"
    return None


def generate_setups(
    df5: pd.DataFrame,
    df1h: pd.DataFrame,
    stop_pct: float = 0.003,
    tp1_pct: float = 0.003,
    tp2_a_pct: float = 0.006,
    tp2_a_plus_pct: float = 0.010,
    runner_a_pct: float = 0.0075,
    runner_a_plus_pct: float = 0.015,
    htf_proximity_pct: float = 0.002,
    a_body_threshold: float = 0.52,
    a_plus_body_threshold: float = 0.70,
    direction_filter: Direction | None = None,
    session_filter: str | None = None,
    use_pips: bool = False,
    pip_value: float = 0.01,
    stop_pips: float = 9.0,
    tp1_pips: float = 9.0,
    tp2_a_pips: float = 20.0,
    tp2_a_plus_pips: float = 35.0,
    runner_a_pips: float = 25.0,
    runner_a_plus_pips: float = 50.0,
    htf_proximity_pips: float = 12.0,
    fvg_lookforward: int = 24,
) -> list[Setup]:
    """Generate all A/A+ setups in the dataset.

    By default targets are percentage-of-price. Set use_pips=True for
    absolute price targets (e.g. 9 pips on XAUUSD where 1 pip = pip_value).
    """
    df1h = compute_htf_levels(df1h)
    df5 = build_htf_map(df1h, df5)
    legs = detect_momentum_legs(df5, min_body_ratio=a_body_threshold)
    setups: list[Setup] = []

    for leg in legs:
        end_idx = leg["end_idx"]
        fvg = find_fvg(df5, end_idx, max_lookforward=fvg_lookforward)
        if fvg is None:
            continue

        direction = fvg["direction"]

        # The FVG must be a continuation of the momentum leg (same direction).
        if fvg["direction"] != leg["direction"]:
            continue

        if direction_filter is not None and direction != direction_filter:
            continue

        entry_price = fvg["midpoint"]
        ts = df5["timestamp"].iloc[fvg["idx"]]

        # Session filter: include Asia by default; restrict when a specific
        # session_filter is requested.
        sess = session_label(ts, include_asia=(session_filter is None or session_filter == "Asia"))
        if session_filter is not None:
            if sess != session_filter:
                continue
        else:
            if sess is None:
                continue

        # HTF confluence
        if use_pips:
            htf_aligned = htf_confluence(
                entry_price, df5.iloc[fvg["idx"]], htf_proximity_pips * pip_value / entry_price
            )
        else:
            htf_aligned = htf_confluence(
                entry_price, df5.iloc[fvg["idx"]], htf_proximity_pct
            )

        # Grading
        min_body = leg["min_body_ratio"]
        if min_body >= a_plus_body_threshold and htf_aligned:
            grade = Grade.A_PLUS
        elif min_body >= a_body_threshold:
            grade = Grade.A
        else:
            continue

        # Determine target distances
        if use_pips:
            stop_dist = stop_pips * pip_value
            tp1_dist = tp1_pips * pip_value
            if grade == Grade.A_PLUS:
                tp2_dist = tp2_a_plus_pips * pip_value
                runner_dist = runner_a_plus_pips * pip_value
            else:
                tp2_dist = tp2_a_pips * pip_value
                runner_dist = runner_a_pips * pip_value
        else:
            stop_dist = entry_price * stop_pct
            tp1_dist = entry_price * tp1_pct
            if grade == Grade.A_PLUS:
                tp2_dist = entry_price * tp2_a_plus_pct
                runner_dist = entry_price * runner_a_plus_pct
            else:
                tp2_dist = entry_price * tp2_a_pct
                runner_dist = entry_price * runner_a_pct

        if direction == Direction.LONG:
            stop_price = entry_price - stop_dist
            tp1_price = entry_price + tp1_dist
            tp2_price = entry_price + tp2_dist
            runner_max_price = entry_price + runner_dist
        else:
            stop_price = entry_price + stop_dist
            tp1_price = entry_price - tp1_dist
            tp2_price = entry_price - tp2_dist
            runner_max_price = entry_price - runner_dist

        setups.append(
            Setup(
                timestamp=ts,
                direction=direction,
                grade=grade,
                entry_price=entry_price,
                stop_price=stop_price,
                tp1_price=tp1_price,
                tp2_price=tp2_price,
                runner_max_price=runner_max_price,
                htf_aligned=htf_aligned,
                momentum_leg_idx=end_idx,
            )
        )

    return setups


def diagnose_latest_bar(
    df5: pd.DataFrame,
    df1h: pd.DataFrame,
    htf_proximity_pct: float = 0.002,
    a_body_threshold: float = 0.52,
    a_plus_body_threshold: float = 0.70,
    fvg_lookforward: int = 24,
    session_filter: str | None = None,
) -> dict:
    """Return a diagnostic dict explaining why the latest closed bar did or did
    not produce an A/A+ setup. Useful for real-time debugging in the bot.
    """
    df1h = compute_htf_levels(df1h)
    df5 = build_htf_map(df1h, df5)
    latest_idx = len(df5) - 1
    latest_ts = df5["timestamp"].iloc[latest_idx]

    result = {
        "timestamp": latest_ts,
        "fvg_completed": False,
        "fvg_direction": None,
        "momentum_leg_found": False,
        "leg_end_idx": None,
        "leg_min_body_ratio": None,
        "leg_swing_breach": False,
        "session_ok": False,
        "htf_aligned": False,
        "htf_distances": {},
        "grade_a_ok": False,
        "grade_a_plus_ok": False,
        "entry_price": None,
        "reasons": [],
    }

    # 1) Session check
    sess = session_label(latest_ts, include_asia=(session_filter is None or session_filter == "Asia"))
    if session_filter is not None:
        result["session_ok"] = sess == session_filter
    else:
        result["session_ok"] = sess is not None
    result["session"] = sess
    if not result["session_ok"]:
        result["reasons"].append(f"session={sess} (outside allowed windows)")

    # 2) FVG completion on latest bar
    if latest_idx < 3:
        result["reasons"].append("not enough history for FVG lookback")
        return result

    h_i2 = df5["high"].iloc[latest_idx - 2]
    l_i2 = df5["low"].iloc[latest_idx - 2]
    h_i = df5["high"].iloc[latest_idx]
    l_i = df5["low"].iloc[latest_idx]

    bullish_fvg = h_i2 < l_i
    bearish_fvg = l_i2 > h_i

    if bullish_fvg:
        result["fvg_completed"] = True
        result["fvg_direction"] = "LONG"
        result["entry_price"] = (h_i2 + l_i) / 2.0
    elif bearish_fvg:
        result["fvg_completed"] = True
        result["fvg_direction"] = "SHORT"
        result["entry_price"] = (l_i2 + h_i) / 2.0
    else:
        result["reasons"].append(
            f"no FVG on this candle (h[i-2]={h_i2:.2f} vs l[i]={l_i:.2f}, "
            f"l[i-2]={l_i2:.2f} vs h[i]={h_i:.2f})"
        )

    # 3) Momentum leg in the valid window: leg end between [latest_idx - lookforward,
    #    latest_idx - 3] (matching find_fvg's search range).
    if result["fvg_completed"]:
        legs = detect_momentum_legs(df5, min_body_ratio=a_body_threshold)
        valid_legs = [
            leg
            for leg in legs
            if latest_idx - fvg_lookforward <= leg["end_idx"] <= latest_idx - 3
        ]
        if valid_legs:
            # Pick the most recent leg
            best_leg = max(valid_legs, key=lambda leg: leg["end_idx"])
            result["momentum_leg_found"] = True
            result["leg_end_idx"] = best_leg["end_idx"]
            result["leg_min_body_ratio"] = best_leg["min_body_ratio"]
            result["grade_a_ok"] = best_leg["min_body_ratio"] >= a_body_threshold
            result["grade_a_plus_ok"] = (
                best_leg["min_body_ratio"] >= a_plus_body_threshold
            )
        else:
            result["reasons"].append(
                f"no valid momentum leg in window [idx-{fvg_lookforward}, idx-3] "
                f"with body ratio >= {a_body_threshold}"
            )

    # 4) HTF confluence
    if result["fvg_completed"] and result["entry_price"] is not None:
        entry_price = result["entry_price"]
        htf_levels = df5.iloc[latest_idx]
        result["htf_aligned"] = htf_confluence(entry_price, htf_levels, htf_proximity_pct)
        for name, level in [
            ("ema20", htf_levels.get("htf_ema20")),
            ("ema50", htf_levels.get("htf_ema50")),
            ("poc30", htf_levels.get("htf_poc30")),
        ]:
            if pd.notna(level):
                result["htf_distances"][name] = abs(entry_price - level) / entry_price
        if not result["htf_aligned"]:
            dists = ", ".join(
                f"{k}={v*100:.2f}%" for k, v in result["htf_distances"].items()
            )
            result["reasons"].append(
                f"HTF confluence failed (distances: {dists}, need <= {htf_proximity_pct*100:.2f}%)"
            )

    # 5) Overall grade / reason
    if result["fvg_completed"] and result["momentum_leg_found"]:
        if not result["grade_a_ok"]:
            result["reasons"].append(
                f"body ratio {result['leg_min_body_ratio']:.2f} below Grade A threshold {a_body_threshold}"
            )
        elif not result["htf_aligned"]:
            result["reasons"].append("Grade A requires HTF alignment for A+")

    return result
