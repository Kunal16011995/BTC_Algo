r"""
Export a complete trade/execution log from Bybit.

What this script does:
- Connects to the Bybit demo account using the API keys in .env
- Downloads every historical execution (fill) for the configured symbol
- Downloads every closed-PnL record (one row per completed trade)
- Downloads every historical order (submitted/cancelled/filled)
- Links each fill to its parent trade's closed PnL via orderId
- Saves all three as timestamped CSV files in the export/ subfolder

Output files (in C:\Algos\Crypto\BTC\export):
- bybit_executions_YYYYMMDD_HHMMSS.csv  -> every fill/execution, with the
  parent trade's closed PnL, avg entry/exit price, qty, and side added
- bybit_closed_pnl_YYYYMMDD_HHMMSS.csv  -> completed round-trip trades with PnL
- bybit_orders_YYYYMMDD_HHMMSS.csv      -> every order (open, closed, cancelled)

Run with:
    python export_bybit_trade_log.py
    python export_bybit_trade_log.py --start 2026-09-30 --end 2026-10-15
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

import config
from bybit_client import BybitClient


def parse_args():
    parser = argparse.ArgumentParser(description='Export Bybit trade/execution history')
    parser.add_argument('--start', type=str, default='2026-09-30',
                        help='Start date in YYYY-MM-DD format (default: 2026-09-30)')
    parser.add_argument('--end', type=str, default=None,
                        help='End date in YYYY-MM-DD format (default: today)')
    return parser.parse_args()


def main():
    args = parse_args()

    if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
        print('ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set in .env')
        sys.exit(1)

    client = BybitClient(
        api_key=config.BYBIT_API_KEY,
        api_secret=config.BYBIT_API_SECRET,
        demo_base_url=config.BYBIT_DEMO_BASE_URL,
        public_base_url=config.BYBIT_PUBLIC_BASE_URL,
    )

    symbol = config.SYMBOL
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'export')
    os.makedirs(out_dir, exist_ok=True)

    # Parse requested date range
    start_dt = datetime.strptime(args.start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.end, '%Y-%m-%d').replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    range_label = f"{args.start}_to_{args.end or end_dt.strftime('%Y-%m-%d')}"
    print(f'Exporting Bybit history for {symbol}')
    print(f'Date range: {start_dt.date()} UTC -> {end_dt.date()} UTC')

    print(f'\nFetching all executions for {symbol} ...')
    executions = client.get_all_executions(symbol, start_ms=start_ms, end_ms=end_ms)
    print(f'Total executions fetched: {len(executions)}')

    print(f'\nFetching all closed PnL for {symbol} ...')
    closed_pnl = client.get_all_closed_pnl(symbol, start_ms=start_ms, end_ms=end_ms)
    print(f'Closed-PnL records fetched: {len(closed_pnl)}')

    print(f'\nFetching all order history for {symbol} ...')
    orders = client.get_all_order_history(symbol, start_ms=start_ms, end_ms=end_ms)
    print(f'Order history records fetched: {len(orders)}')

    exec_file = os.path.join(out_dir, f'bybit_executions_{range_label}_{ts}.csv')
    pnl_file = os.path.join(out_dir, f'bybit_closed_pnl_{range_label}_{ts}.csv')
    order_file = os.path.join(out_dir, f'bybit_orders_{range_label}_{ts}.csv')

    df_pnl = pd.DataFrame()
    if closed_pnl:
        df_pnl = pd.DataFrame(closed_pnl)
        if 'updatedTime' in df_pnl.columns:
            df_pnl['updatedTime_dt'] = pd.to_datetime(df_pnl['updatedTime'].astype(float), unit='ms', utc=True)
        if 'createdTime' in df_pnl.columns:
            df_pnl['createdTime_dt'] = pd.to_datetime(df_pnl['createdTime'].astype(float), unit='ms', utc=True)
        df_pnl.to_csv(pnl_file, index=False)
        print(f'Saved closed PnL to: {pnl_file}')
    else:
        print('No closed PnL found on this account.')

    if orders:
        df_orders = pd.DataFrame(orders)
        if 'createdTime' in df_orders.columns:
            df_orders['createdTime_dt'] = pd.to_datetime(df_orders['createdTime'].astype(float), unit='ms', utc=True)
        if 'updatedTime' in df_orders.columns:
            df_orders['updatedTime_dt'] = pd.to_datetime(df_orders['updatedTime'].astype(float), unit='ms', utc=True)
        df_orders.to_csv(order_file, index=False)
        print(f'Saved order history to: {order_file}')
    else:
        print('No order history found on this account.')

    if executions:
        df_exec = pd.DataFrame(executions)

        # Convert timestamp columns to readable UTC datetime
        if 'execTime' in df_exec.columns:
            df_exec['execTime_dt'] = pd.to_datetime(df_exec['execTime'].astype(float), unit='ms', utc=True)
        if 'createdTime' in df_exec.columns:
            df_exec['createdTime_dt'] = pd.to_datetime(df_exec['createdTime'].astype(float), unit='ms', utc=True)

        # Link each fill to its parent closed-PnL trade via orderId
        if not df_pnl.empty and 'orderId' in df_exec.columns and 'orderId' in df_pnl.columns:
            pnl_lookup = df_pnl.set_index('orderId')
            df_exec['trade_closedPnl'] = df_exec['orderId'].map(pnl_lookup['closedPnl'])
            df_exec['trade_avgEntryPrice'] = df_exec['orderId'].map(pnl_lookup['avgEntryPrice'])
            df_exec['trade_avgExitPrice'] = df_exec['orderId'].map(pnl_lookup['avgExitPrice'])
            df_exec['trade_qty'] = df_exec['orderId'].map(pnl_lookup['qty'])
            df_exec['trade_side'] = df_exec['orderId'].map(pnl_lookup['side'])
            df_exec['trade_execType'] = df_exec['orderId'].map(pnl_lookup['execType'])

            # Move the trade-PnL columns near the front for readability
            front_cols = [
                'execTime_dt', 'side', 'execPrice', 'execQty', 'execValue',
                'trade_closedPnl', 'trade_avgEntryPrice', 'trade_avgExitPrice',
                'trade_qty', 'trade_side', 'orderId', 'execId'
            ]
            front_cols = [c for c in front_cols if c in df_exec.columns]
            other_cols = [c for c in df_exec.columns if c not in front_cols]
            df_exec = df_exec[front_cols + other_cols]

        df_exec.to_csv(exec_file, index=False)
        print(f'Saved executions to: {exec_file}')
    else:
        print('No executions found on this account.')

    print('\nDone.')


if __name__ == '__main__':
    main()
