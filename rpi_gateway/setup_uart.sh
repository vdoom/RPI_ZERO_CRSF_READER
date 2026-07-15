#!/usr/bin/env bash
# Configure the Raspberry Pi PL011 UART (/dev/ttyAMA0) for CRSF input.
# Run on the Pi itself:  sudo bash setup_uart.sh
# A reboot is required afterwards.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

# Newer Raspberry Pi OS keeps boot files under /boot/firmware.
if [[ -f /boot/firmware/config.txt ]]; then
    CONFIG=/boot/firmware/config.txt
    CMDLINE=/boot/firmware/cmdline.txt
else
    CONFIG=/boot/config.txt
    CMDLINE=/boot/cmdline.txt
fi

echo "==> Using $CONFIG and $CMDLINE"

add_config_line() {
    local line="$1"
    if grep -qxF "$line" "$CONFIG"; then
        echo "    already present: $line"
    else
        echo "$line" >> "$CONFIG"
        echo "    added: $line"
    fi
}

echo "==> Enabling PL011 UART, moving Bluetooth off it"
add_config_line "enable_uart=1"
add_config_line "dtoverlay=disable-bt"

echo "==> Removing serial console from kernel cmdline"
cp "$CMDLINE" "$CMDLINE.bak"
sed -i -E 's/console=serial0,[0-9]+ ?//; s/console=ttyAMA0,[0-9]+ ?//' "$CMDLINE"
if cmp -s "$CMDLINE" "$CMDLINE.bak"; then
    echo "    no serial console entry found (nothing to remove)"
else
    echo "    removed (backup at $CMDLINE.bak)"
fi

echo "==> Disabling hciuart (Bluetooth modem service on the UART)"
systemctl disable --now hciuart 2>/dev/null || true

echo
echo "Done. Reboot now:  sudo reboot"
echo "After reboot, /dev/ttyAMA0 is the PL011 on GPIO14/15 (phys pins 8/10)."
