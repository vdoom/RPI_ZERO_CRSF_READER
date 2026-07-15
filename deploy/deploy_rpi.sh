#!/usr/bin/env bash
# Deploy the ground-side CRSF gateway to the Raspberry Pi over SSH.
#
# Usage:
#   RPI_HOST=pi@192.168.1.10 ./deploy/deploy_rpi.sh [--with-uart-setup] [--run-tests]
#
# Env vars:
#   RPI_HOST     ssh target                (default pi@192.168.1.10)
#   INSTALL_DIR  install path on the Pi    (default /opt/crsf-link)
set -euo pipefail

RPI_HOST="${RPI_HOST:-pi@192.168.1.10}"
INSTALL_DIR="${INSTALL_DIR:-/opt/crsf-link}"
STAGE="/tmp/crsf-link-stage"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WITH_UART_SETUP=0
RUN_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --with-uart-setup) WITH_UART_SETUP=1 ;;
        --run-tests)       RUN_TESTS=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

echo "==> Syncing sources to $RPI_HOST:$STAGE"
rsync -az --delete \
    --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
    "$REPO_ROOT/protocol" "$REPO_ROOT/rpi_gateway" "$REPO_ROOT/tools" \
    "$REPO_ROOT/tests" "$REPO_ROOT/conftest.py" \
    "$REPO_ROOT/requirements-rpi.txt" \
    "$RPI_HOST:$STAGE/"

echo "==> Installing on $RPI_HOST (sudo required on the Pi)"
ssh "$RPI_HOST" "INSTALL_DIR='$INSTALL_DIR' STAGE='$STAGE' bash -s" <<'REMOTE'
set -euo pipefail
sudo mkdir -p "$INSTALL_DIR"
sudo rsync -a --delete --exclude 'rpi_gateway/config.yaml' "$STAGE/" "$INSTALL_DIR/"
# Keep an existing (possibly edited) config; install the default only once.
if ! sudo test -f "$INSTALL_DIR/rpi_gateway/config.yaml"; then
    sudo cp "$STAGE/rpi_gateway/config.yaml" "$INSTALL_DIR/rpi_gateway/config.yaml"
fi
if ! python3 -c 'import serial, yaml' 2>/dev/null; then
    echo "==> Installing python dependencies"
    sudo apt-get update
    sudo apt-get install -y python3-serial python3-yaml \
        || sudo pip3 install --break-system-packages -r "$INSTALL_DIR/requirements-rpi.txt"
fi
sudo install -m 644 "$INSTALL_DIR/rpi_gateway/crsf-gateway.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crsf-gateway.service
sudo systemctl --no-pager --lines 5 status crsf-gateway.service || true
REMOTE

if [[ $WITH_UART_SETUP -eq 1 ]]; then
    echo "==> Running UART setup (a reboot will be required)"
    ssh "$RPI_HOST" "sudo bash '$INSTALL_DIR/rpi_gateway/setup_uart.sh'"
fi

if [[ $RUN_TESTS -eq 1 ]]; then
    echo "==> Running unit tests on the Pi"
    ssh "$RPI_HOST" "command -v pytest >/dev/null 2>&1 \
        || sudo apt-get install -y python3-pytest; \
        cd '$INSTALL_DIR' && python3 -m pytest rpi_gateway/tests tests -q"
fi

echo "==> Done. Logs: ssh $RPI_HOST journalctl -u crsf-gateway -f"
