#!/usr/bin/env bash
set -euo pipefail

# Install systemd services for the BTC FVG momentum project.
# Run as a user with sudo privileges.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

if [ "$USER_NAME" = "root" ] && [ -z "${SUDO_USER:-}" ]; then
    echo "Warning: installing services for root user. Set SUDO_USER to use a normal user."
fi

PYTHON_BIN="${PROJECT_ROOT}/venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

echo "Project root: ${PROJECT_ROOT}"
echo "User:         ${USER_NAME}"
echo "Python:       ${PYTHON_BIN}"

mkdir -p "${PROJECT_ROOT}/logs"

for service in dashboard bot; do
    template="${PROJECT_ROOT}/systemd/btc-fvg-${service}.service.template"
    output="${PROJECT_ROOT}/systemd/btc-fvg-${service}.service"
    sed \
        -e "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" \
        -e "s|{{USER}}|${USER_NAME}|g" \
        -e "s|{{PYTHON}}|${PYTHON_BIN}|g" \
        "$template" > "$output"
    echo "Generated ${output}"
done

echo
echo "To install the services, run:"
echo "  sudo cp ${PROJECT_ROOT}/systemd/btc-fvg-*.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now btc-fvg-dashboard.service"
echo "  sudo systemctl enable --now btc-fvg-bot.service"
echo
echo "To check status:"
echo "  sudo systemctl status btc-fvg-dashboard.service"
echo "  sudo systemctl status btc-fvg-bot.service"
echo
echo "To view logs:"
echo "  sudo journalctl -u btc-fvg-dashboard.service -f"
echo "  sudo journalctl -u btc-fvg-bot.service -f"
