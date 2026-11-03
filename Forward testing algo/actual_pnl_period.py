import store
import pandas as pd

trades = store.load_trades_variant("tight")
start = pd.Timestamp("2026-10-15", tz="UTC")
end = pd.Timestamp("2026-10-31", tz="UTC")
filtered = []
for t in trades:
    ts = pd.to_datetime(t["exit_ts"], unit="ms", utc=True)
    if start <= ts <= end:
        filtered.append(t)

print(f"Actual bot trades {start.date()} to {end.date()}: {len(filtered)}")
pnls = [t["pnl"] for t in filtered]
print(f"Net PnL: ${sum(pnls):,.2f}")
print(f"Win rate: {sum(1 for p in pnls if p > 0) / len(pnls) * 100:.1f}%")
for t in filtered[-10:]:
    ts = pd.to_datetime(t["exit_ts"], unit="ms", utc=True)
    print(
        f"{ts.strftime('%Y-%m-%d %H:%M')} {t['side']:<4} "
        f"{t['entry_price']:>10.2f} {t['exit_price']:>10.2f} PnL={t['pnl']:>10.2f}"
    )
