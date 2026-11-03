import json
from datetime import datetime, timezone

with open('data/fvg_backtest_tight.json') as f:
    data = json.load(f)

trades = data.get('trades', [])
kpis = data.get('kpis', {})

print('Backtest tab current data (before regeneration):')
print(f'Trades: {len(trades)}')
print(f'Period: {datetime.fromtimestamp(trades[0]["entry_ts"]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")} -> {datetime.fromtimestamp(trades[-1]["exit_ts"]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC')
print(f'Capital: ${data.get("capital", 0):,.2f}')
print(f'Final equity: ${kpis.get("final_equity", 0):,.2f}')
print(f'Total return: {kpis.get("total_return", 0):.2f}%')
print(f'Win rate: {kpis.get("win_rate", 0):.1f}%')
print(f'Profit factor: {kpis.get("profit_factor", 0):.2f}')
print(f'Sharpe: {kpis.get("sharpe_ratio", 0):.2f}')
print(f'Max DD: {kpis.get("max_drawdown", 0):.2f}%')
print()
print('First 10 trades:')
print(f'{"Entry":<20} {"Exit":<20} {"Side":<6} {"Entry":>10} {"Exit":>10} {"PnL":>10} {"Reason":<12}')
for t in trades[:10]:
    e = datetime.fromtimestamp(t['entry_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    x = datetime.fromtimestamp(t['exit_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    r = t.get('raw', {})
    print(f'{e:<20} {x:<20} {t["side"]:<6} {t["entry_price"]:>10.2f} {t["exit_price"]:>10.2f} {t["pnl"]:>10.2f} {r.get("exit_reason", ""):<12}')
print()
print('Last 10 trades:')
for t in trades[-10:]:
    e = datetime.fromtimestamp(t['entry_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    x = datetime.fromtimestamp(t['exit_ts']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    r = t.get('raw', {})
    print(f'{e:<20} {x:<20} {t["side"]:<6} {t["entry_price"]:>10.2f} {t["exit_price"]:>10.2f} {t["pnl"]:>10.2f} {r.get("exit_reason", ""):<12}')
