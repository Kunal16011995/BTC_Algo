# BTC Algorithmic Trading Framework

Systematic trading framework for BTCUSDT on Bybit, currently running two strategies:

1. **FVG Momentum** (`src/fvg/`) — SSRN-6725880 A/A+ Fair Value Gap momentum strategy.
2. **WD Gann Overnight** (`src/gann/`) — Green/Red weekly trend-line strategy.

## Repository layout

```text
BTC_Algo/
├── src/                    <- exact production code only
│   ├── shared/             <- utilities used by every strategy
│   │   ├── bybit_client.py
│   │   ├── config.py
│   │   └── store.py
│   ├── fvg/                <- FVG momentum strategy
│   │   ├── bot.py              live/paper forward-test bot
│   │   ├── dashboard.py        Flask dashboard
│   │   ├── strategy.py         signal engine
│   │   ├── backtest.py         backtest engine
│   │   ├── compute_backtest.py regenerates dashboard backtest JSON
│   │   └── templates/
│   ├── gann/               <- WD Gann strategy
│   │   ├── forward_bot.py
│   │   ├── strategy.py
│   │   └── backtest.py
│   └── research/           <- diagnostics, optimisations, comparisons
├── scripts/                <- batch-file entry points
│   ├── run_all.bat
│   ├── start_fvg_bot.bat
│   ├── start_gann_bot.bat
│   ├── run_dashboard.bat
│   └── run_fvg_forward_test.bat
├── docs/                   <- documentation and change log
│   ├── change_log.txt
│   └── strategy_explainer.txt
├── data/                   <- runtime data (gitignored)
├── logs/                   <- runtime logs (gitignored)
├── export/                 <- trade exports (gitignored)
└── requirements.txt
```

## First-time setup

1. Install Python dependencies:

   ```bat
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your Bybit API keys.

3. Create runtime directories:

   ```bat
   mkdir data logs export
   ```

## Running the system

All batch files set `PYTHONPATH` to the project root automatically, so run them from anywhere.

### Run everything (dashboard + FVG bot + Gann bot)

```bat
scripts\run_all.bat
```

### Run only the FVG momentum forward-test bot

```bat
scripts\start_fvg_bot.bat
```

### Run only the WD Gann forward-test bot

```bat
scripts\start_gann_bot.bat
```

### Run only the dashboard

```bat
scripts\run_dashboard.bat
```

## Regenerating backtest data

```bat
python -m src.fvg.compute_backtest
python -m src.research.generate_a3_dashboard_json
```

## What is "exact code" vs "helper code"

- **Exact / production code**: everything under `src/`. These are the files that run live trades and the dashboard.
- **Helper / research code**: everything under `src/research/`. These are diagnostics, optimisations, and one-off analyses. They import from `src/` but are not part of the live system.
- **Entry points**: everything under `scripts/`.

## Notes

- Sensitive files (`.env`, `bot.db`, `logs/`, `export/`, `data/`) are gitignored.
- The FVG momentum bot and WD Gann bot trade BTCUSDT in one-way mode. They use separate Bybit demo accounts so they can run simultaneously.
