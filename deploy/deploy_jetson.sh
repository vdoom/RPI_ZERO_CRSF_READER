#!/usr/bin/env bash
# Deploy the air-side MAVLink bridge to the Jetson Orin Nano over SSH.
#
# Usage:
#   JETSON_HOST=user@192.168.1.20 ./deploy/deploy_jetson.sh [--run-tests]
#
# Env vars:
#   JETSON_HOST  ssh target                 (default jetson@192.168.1.20)
#   INSTALL_DIR  install path on the Jetson (default /opt/crsf-link)
set -euo pipefail

JETSON_HOST="${JETSON_HOST:-jetson@192.168.1.20}"
INSTALL_DIR="${INSTALL_DIR:-/opt/crsf-link}"
STAGE="/tmp/crsf-link-stage"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUN_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --run-tests) RUN_TESTS=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

echo "==> Syncing sources to $JETSON_HOST:$STAGE"
rsync -az --delete \
    --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
    "$REPO_ROOT/protocol" "$REPO_ROOT/jetson_bridge" "$REPO_ROOT/tools" \
    "$REPO_ROOT/tests" "$REPO_ROOT/conftest.py" \
    "$REPO_ROOT/requirements-jetson.txt" \
    "$JETSON_HOST:$STAGE/"

echo "==> Installing on $JETSON_HOST (sudo required on the Jetson)"
ssh "$JETSON_HOST" "INSTALL_DIR='$INSTALL_DIR' STAGE='$STAGE' bash -s" <<'REMOTE'
set -euo pipefail
sudo mkdir -p "$INSTALL_DIR"
sudo rsync -a --delete --exclude 'jetson_bridge/config.yaml' "$STAGE/" "$INSTALL_DIR/"
# Keep an existing (possibly edited) config; install the default only once.
if ! sudo test -f "$INSTALL_DIR/jetson_bridge/config.yaml"; then
    sudo cp "$STAGE/jetson_bridge/config.yaml" "$INSTALL_DIR/jetson_bridge/config.yaml"
fi
if ! python3 -c 'import pymavlink, yaml' 2>/dev/null; then
    echo "==> Installing python dependencies"
    sudo apt-get update && sudo apt-get install -y python3-pip python3-yaml || true
    sudo pip3 install -r "$INSTALL_DIR/requirements-jetson.txt" \
        || sudo pip3 install --break-system-packages -r "$INSTALL_DIR/requirements-jetson.txt"
fi
# The bridge needs the UART; make sure nvgetty is not holding /dev/ttyTHS1.
if systemctl is-enabled nvgetty >/dev/null 2>&1; then
    echo "==> Disabling nvgetty (serial console on /dev/ttyTHS1)"
    sudo systemctl disable --now nvgetty
fi
sudo install -m 644 "$INSTALL_DIR/jetson_bridge/mavlink-bridge.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mavlink-bridge.service
sudo systemctl --no-pager --lines 5 status mavlink-bridge.service || true
REMOTE

if [[ $RUN_TESTS -eq 1 ]]; then
    echo "==> Running unit tests on the Jetson"
    ssh "$JETSON_HOST" "python3 -m pytest --version >/dev/null 2>&1 \
        || sudo apt-get install -y python3-pytest; \
        cd '$INSTALL_DIR' && python3 -m pytest jetson_bridge/tests tests -q"
fi

echo "==> Done. Logs: ssh $JETSON_HOST journalctl -u mavlink-bridge -f"
