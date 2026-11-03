#!/usr/bin/env bash
set -euo pipefail

# Project root is one level above scripts/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

export PYTHONPATH="${PROJECT_ROOT}"

mkdir -p logs data

export DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-5556}"

echo "Starting BTC dashboard on http://${DASHBOARD_HOST}:${DASHBOARD_PORT} ..."
python -m src.fvg.dashboard
