#!/usr/bin/env bash
set -euo pipefail

# Configure Telegram alerts for the BTC FVG bot.
# Usage: bash scripts/setup_telegram.sh "BOT_TOKEN" "CHAT_ID"

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <TELEGRAM_BOT_TOKEN> <CHAT_ID>"
    exit 1
fi

TOKEN="$1"
CHAT_ID="$2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

echo "Updating .env with Telegram credentials..."
sed -i '/^TELEGRAM_/d' .env
echo "TELEGRAM_BOT_TOKEN=${TOKEN}" >> .env
echo "TELEGRAM_CHAT_ID=${CHAT_ID}" >> .env

echo "Testing Telegram notification..."
/opt/trading-bot/venv/bin/python -c "
from src.shared import notify
notify.send('<b>Test alert</b>\nTelegram notifications are configured.', subject='Test Alert')
"

echo "Restarting bot..."
sudo systemctl restart btc-fvg-bot.service
sleep 2
sudo systemctl status btc-fvg-bot.service --no-pager
