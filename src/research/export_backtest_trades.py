"""Export all backtest trades to CSV for easy viewing."""
import json
import csv
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path("data/fvg_backtest_tight.json")
OUT_FILE = Path("export/fvg_backtest_tight_trades.csv")

with open(DATA_FILE) as f:
    data = json.load(f)

trades = data.get("trades", [])
OUT_FILE.parent.mkdir(exist_ok=True)

with open(OUT_FILE, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "entry_time", "exit_time", "side", "entry_price", "exit_price",
        "qty", "size_usd", "pnl", "gross_pnl", "cost", "result_r",
        "grade", "exit_reason"
    ])
    for t in trades:
        raw = t.get("raw", {})
        entry_ts = int(t.get("entry_ts", 0) or 0) / 1000
        exit_ts = int(t.get("exit_ts", 0) or 0) / 1000
        writer.writerow([
            datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            datetime.fromtimestamp(exit_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            t.get("side", ""),
            t.get("entry_price", ""),
            t.get("exit_price", ""),
            t.get("qty", ""),
            raw.get("size_usd", ""),
            t.get("pnl", ""),
            raw.get("gross_pnl", ""),
            raw.get("cost", ""),
            raw.get("result_r", ""),
            raw.get("grade", ""),
            raw.get("exit_reason", ""),
        ])

print(f"Exported {len(trades)} trades to {OUT_FILE}")
