"""Compare backtest vs corrected forward-test trades for Oct 20-30, 2026."""
import json
from datetime import datetime, timezone

import pandas as pd

# Backtest trades from updated JSON
with open("data/fvg_backtest_tight.json") as f:
    bt = json.load(f)

bt_trades = bt.get("trades", [])
start_ms = int(pd.Timestamp("2026-10-20 00:00", tz="UTC").timestamp() * 1000)
end_ms = int(pd.Timestamp("2026-11-04 00:00", tz="UTC").timestamp() * 1000)

bt_oct = [
    t for t in bt_trades
    if start_ms <= int(t.get("entry_ts", 0) or 0) < end_ms
]

print("=" * 110)
print("BACKTEST trades: 20 Oct 2026 -> 03 Nov 2026")
print("=" * 110)
print(f"{'Entry':<18} {'Exit':<18} {'Side':<6} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Reason':<12}")
print("-" * 110)
total_bt = 0.0
for t in bt_oct:
    e = datetime.fromtimestamp(t["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    x = datetime.fromtimestamp(t["exit_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    r = t.get("raw", {})
    print(f"{e:<18} {x:<18} {t['side']:<6} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} {t['pnl']:>10.2f} {r.get('exit_reason', ''):<12}")
    total_bt += t["pnl"]

wins_bt = sum(1 for t in bt_oct if t["pnl"] > 0)
losses_bt = len(bt_oct) - wins_bt
print("-" * 110)
print(f"Trades: {len(bt_oct)} | Wins: {wins_bt} | Losses: {losses_bt} | Net PnL: ${total_bt:,.2f}")
print()

# Corrected forward-test trades (from corrected_signals_pnl.py output)
fw_oct = [
    {"entry_ts": "2026-10-20 07:15", "exit_ts": "2026-10-20 08:00", "side": "Sell", "entry": 81727.65, "exit": 81513.30, "pnl": 222.20, "reason": "trail_1r"},
    {"entry_ts": "2026-10-22 02:30", "exit_ts": "2026-10-22 03:00", "side": "Sell", "entry": 83103.50, "exit": 82771.09, "pnl": 381.37, "reason": "trail_1r"},
    {"entry_ts": "2026-10-22 08:30", "exit_ts": "2026-10-22 09:00", "side": "Sell", "entry": 82037.85, "exit": 81709.70, "pnl": 381.37, "reason": "trail_1r"},
    {"entry_ts": "2026-10-25 09:15", "exit_ts": "2026-10-25 11:45", "side": "Sell", "entry": 80291.45, "exit": 80612.62, "pnl": -543.16, "reason": "stop_loss"},
    {"entry_ts": "2026-10-29 01:45", "exit_ts": "2026-10-29 02:00", "side": "Sell", "entry": 80686.90, "exit": 80147.95, "pnl": 691.03, "reason": "trail_1r"},
    {"entry_ts": "2026-10-29 14:45", "exit_ts": "2026-10-29 15:00", "side": "Buy", "entry": 80620.00, "exit": 81152.30, "pnl": 682.14, "reason": "trail_1r"},
    {"entry_ts": "2026-10-30 00:30", "exit_ts": "2026-10-30 00:45", "side": "Sell", "entry": 77548.90, "exit": 77079.60, "pnl": 618.48, "reason": "trail_1r"},
    {"entry_ts": "2026-10-30 04:30", "exit_ts": "2026-10-30 05:00", "side": "Buy", "entry": 78214.80, "exit": 78229.00, "pnl": -59.92, "reason": "trail_1r"},
    {"entry_ts": "2026-11-03 07:00", "exit_ts": "2026-11-03 07:30", "side": "Sell", "entry": 76613.75, "exit": 76920.21, "pnl": -543.16, "reason": "stop_loss"},
    {"entry_ts": "2026-11-03 11:00", "exit_ts": "2026-11-03 12:15", "side": "Buy", "entry": 77049.85, "exit": 77156.20, "pnl": 78.62, "reason": "trail_1r"},
]

print("=" * 110)
print("CORRECTED FORWARD-TEST trades: 20 Oct 2026 -> 03 Nov 2026")
print("=" * 110)
print(f"{'Entry':<18} {'Exit':<18} {'Side':<6} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Reason':<12}")
print("-" * 110)
total_fw = 0.0
for t in fw_oct:
    print(f"{t['entry_ts']:<18} {t['exit_ts']:<18} {t['side']:<6} {t['entry']:>10.2f} {t['exit']:>10.2f} {t['pnl']:>10.2f} {t['reason']:<12}")
    total_fw += t["pnl"]

wins_fw = sum(1 for t in fw_oct if t["pnl"] > 0)
losses_fw = len(fw_oct) - wins_fw
print("-" * 110)
print(f"Trades: {len(fw_oct)} | Wins: {wins_fw} | Losses: {losses_fw} | Net PnL: ${total_fw:,.2f}")
print()

print("=" * 110)
print("SIDE-BY-SIDE COMPARISON")
print("=" * 110)
print(f"{'Metric':<30} {'Backtest':>20} {'Corrected Forward':>20}")
print("-" * 110)
print(f"{'Trades':<30} {len(bt_oct):>20} {len(fw_oct):>20}")
print(f"{'Win rate':<30} {wins_bt/len(bt_oct)*100 if bt_oct else 0:>19.1f}% {wins_fw/len(fw_oct)*100 if fw_oct else 0:>19.1f}%")
print(f"{'Net PnL':<30} ${total_bt:>18,.2f} ${total_fw:>18,.2f}")
print(f"{'Avg PnL/trade':<30} ${total_bt/len(bt_oct) if bt_oct else 0:>18,.2f} ${total_fw/len(fw_oct) if fw_oct else 0:>18,.2f}")
