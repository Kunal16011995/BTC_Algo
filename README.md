# BTC Algorithmic Trading Framework

Systematic trading framework for BTCUSDT on Bybit, running the **FVG Momentum** strategy (`src/fvg/`) — SSRN-6725880 A/A+ Fair Value Gap momentum strategy.

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
│   └── research/           <- diagnostics, optimisations, comparisons
├── scripts/                <- entry points (Windows .bat + Linux .sh)
│   ├── run_all.bat / run_all.sh
│   ├── start_fvg_bot.bat / run_fvg_bot.sh
│   ├── run_dashboard.bat / run_dashboard.sh
│   ├── run_fvg_forward_test.bat
│   └── install_systemd.sh
├── systemd/                <- systemd service templates for VPS deployment
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

   ```bash
   pip install -r requirements.txt
   ```

   For production/VPS deployment a virtual environment is recommended:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your Bybit API keys.

3. Create runtime directories:

   ```bash
   mkdir data logs export
   ```

## Running on Windows

All batch files set `PYTHONPATH` to the project root automatically.

### Run everything (dashboard + FVG bot)

```bat
scripts\run_all.bat
```

### Run only the FVG momentum forward-test bot

```bat
scripts\start_fvg_bot.bat
```

### Run only the dashboard

```bat
scripts\run_dashboard.bat
```

## Running on Linux / Ubuntu VPS

Make the shell scripts executable once:

```bash
chmod +x scripts/*.sh
```

### Quick start (background, non-persistent across reboots)

```bash
bash scripts/run_all.sh
```

### Run only the dashboard

```bash
bash scripts/run_dashboard.sh
```

The dashboard binds to `0.0.0.0:5556` by default on Linux so it is reachable
from outside the VPS. Override with environment variables if needed:

```bash
DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8080 bash scripts/run_dashboard.sh
```

### Run only the FVG momentum bot

```bash
bash scripts/run_fvg_bot.sh
```

### Persistent systemd service setup

Generate and install systemd services (run from the project root):

```bash
bash scripts/install_systemd.sh
sudo cp systemd/btc-fvg-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc-fvg-dashboard.service
sudo systemctl enable --now btc-fvg-bot.service
```

Check status and logs:

```bash
sudo systemctl status btc-fvg-dashboard.service
sudo systemctl status btc-fvg-bot.service
sudo journalctl -u btc-fvg-dashboard.service -f
sudo journalctl -u btc-fvg-bot.service -f
```

## VPS firewall notes

If you want to view the dashboard remotely, open TCP port 5556:

```bash
sudo ufw allow 5556/tcp
```

The bot health endpoint runs on port 5557; open it only if you want external
monitoring of that port.

## Notifications (Telegram / Email)

The bot can send alerts on every **entry**, **SL trail**, **TP hit**, and **SL hit**
for both live and paper trades. It also sends a periodic **heartbeat** status
message (default every 15 minutes) so you know the bot is still alive and
scanning M15 bars even when no trades are placed.

1. Copy `.env.example` to `.env` and fill in either Telegram or SMTP details:

   ```bash
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   ```

   To get a Telegram token, message [@BotFather](https://t.me/BotFather).
   To get your chat ID, message [@userinfobot](https://t.me/userinfobot) or open
   `https://api.telegram.org/bot<token>/getUpdates` after messaging your bot.

2. Adjust the heartbeat interval if desired (default 900 seconds = 15 minutes,
   set to `0` to disable):

   ```bash
   HEARTBEAT_INTERVAL_SECONDS=900
   ```

3. Restart the bot:

   ```bash
   sudo systemctl restart btc-fvg-bot.service
   ```

Leave all notification env vars blank to disable alerts.

## Regenerating backtest data

```bash
python -m src.fvg.compute_backtest
```

## What is "exact code" vs "helper code"

- **Exact / production code**: everything under `src/fvg/` and `src/shared/`. These are the files that run live trades and the dashboard.
- **Helper / research code**: everything under `src/research/`. These are diagnostics, optimisations, and one-off analyses. They import from `src/` but are not part of the live system.
- **Entry points**: everything under `scripts/`.

## Notes

- Sensitive files (`.env`, `bot.db`, `logs/`, `export/`, `data/`) are gitignored.
- The FVG momentum bot trades BTCUSDT in one-way mode on the Bybit demo account.
