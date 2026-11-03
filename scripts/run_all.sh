#!/usr/bin/env bash
set -euo pipefail

# Project root is one level above scripts/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

export PYTHONPATH="${PROJECT_ROOT}"

mkdir -p logs data

echo "============================================================"
echo " BTC Momentum launcher (dashboard + FVG bot)"
echo "============================================================"
echo "Dashboard: http://0.0.0.0:5556"
echo "Bot health: http://0.0.0.0:5557/health"
echo "============================================================"
echo

# Kill any stale bot/dashboard processes before starting
pkill -f 'src.fvg.bot' 2>/dev/null || true
pkill -f 'src.fvg.dashboard' 2>/dev/null || true

# Wait briefly so ports are released
sleep 3

# Start dashboard in background
nohup python -m src.fvg.dashboard > logs/dashboard.log 2>&1 &
echo "Dashboard PID: $!"

# Give dashboard a moment to start
sleep 2

# Start bot in background
nohup bash "${SCRIPT_DIR}/run_fvg_bot.sh" > logs/fvg_bot_wrapper.log 2>&1 &
echo "Bot wrapper PID: $!"

echo
echo "Both services started in the background."
echo "Check logs/ for output."
echo "Stop with: pkill -f 'src.fvg.bot'; pkill -f 'src.fvg.dashboard'"
