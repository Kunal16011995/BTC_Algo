#!/usr/bin/env bash
set -euo pipefail

# Project root is one level above scripts/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

export PYTHONPATH="${PROJECT_ROOT}"

mkdir -p logs data

echo "Starting FVG momentum paper bot..."
echo "Health: http://0.0.0.0:5557/health"

# Auto-restart loop
while true; do
    echo "[+] Starting FVG paper bot at $(date -u +'%Y-%m-%d %H:%M:%S UTC') ..."
    python -m src.fvg.bot
    exitcode=$?

    # Ctrl+C or clean exit -> stop the wrapper
    if [ "$exitcode" -eq 0 ] || [ "$exitcode" -eq 130 ]; then
        echo "Bot stopped intentionally."
        exit 0
    fi

    echo "[!] Bot crashed with exit code ${exitcode}. Restarting in 10 seconds..."
    sleep 10
done
