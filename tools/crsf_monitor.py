"""Live CRSF monitor: show what the UART is receiving from the radio.

Read-only diagnostic for bench bring-up - it does not send UDP or touch the
FC. Opens the serial port, parses CRSF frames, and prints the decoded 16
channels live, updating in place. It always shows the raw byte rate too, so
you can tell wiring/baud problems apart from decode problems:

  bytes/s == 0            -> nothing on the wire: check wiring (signal on
                            phys pin 10, GND on pin 6), that setup_uart.sh
                            ran + rebooted, and the serial console is off.
  bytes/s > 0, frames 0   -> bytes arrive but nothing decodes: usually wrong
                            baud (must match the Taranis) or a non-CRSF
                            signal. Watch crc_err.
  frames/s ~ 50-150       -> healthy; channels below should move with sticks.

Usage (on the Pi, after deploy the file is at /opt/crsf-link/tools/):
  python3 tools/crsf_monitor.py                 # /dev/ttyAMA0 @ 921600, raw values
  python3 tools/crsf_monitor.py --us            # show channels in microseconds
  python3 tools/crsf_monitor.py -p /dev/ttyAMA0 -b 416666
  python3 tools/crsf_monitor.py --raw           # hex sample of the byte stream
"""

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jetson_bridge.channel_scaler import scale_channels  # noqa: E402
from rpi_gateway.crsf_parser import (  # noqa: E402
    DEFAULT_SYNC_BYTES,
    FRAME_TYPE_RC_CHANNELS_PACKED,
    RC_PAYLOAD_LEN,
    CrsfParser,
    unpack_channels,
)


def open_serial(port, baud):
    import serial
    try:
        return serial.Serial(port, baud, timeout=0.05)
    except (serial.SerialException, OSError) as exc:
        print(f"error: cannot open {port} @ {baud}: {exc}", file=sys.stderr)
        print("hint: run rpi_gateway/setup_uart.sh and reboot; check the "
              "device path and that nothing else holds the port.",
              file=sys.stderr)
        raise SystemExit(1)


def _draw(lines, first):
    if not first:
        sys.stdout.write(f"\033[{len(lines)}A")  # cursor up, overwrite block
    for line in lines:
        sys.stdout.write("\r\033[K" + line + "\n")  # clear to EOL, then text
    sys.stdout.flush()


def run_decoded(ser, port, baud, show_us):
    parser = CrsfParser()
    total_bytes = 0
    window_bytes = 0
    window_frames = 0
    channels = None
    last_refresh = time.monotonic()
    window_start = last_refresh
    first = True

    while True:
        data = ser.read(256)
        if data:
            total_bytes += len(data)
            window_bytes += len(data)
            for frame in parser.feed(data):
                window_frames += 1
                if (frame.frame_type == FRAME_TYPE_RC_CHANNELS_PACKED
                        and len(frame.payload) == RC_PAYLOAD_LEN):
                    channels = unpack_channels(frame.payload)

        now = time.monotonic()
        if now - last_refresh < 0.2:
            continue
        dt = now - window_start
        fps = window_frames / dt if dt > 0 else 0.0
        bps = window_bytes / dt if dt > 0 else 0.0
        window_bytes = window_frames = 0
        window_start = last_refresh = now

        header = f"CRSF monitor  {port} @ {baud}   (Ctrl+C to quit)"
        stats = (f"  frames:{fps:6.1f}/s   bytes:{bps:7.0f}/s   "
                 f"crc_err:{parser.stats.crc_errors}   "
                 f"discarded:{parser.stats.bytes_discarded}   "
                 f"total:{total_bytes}")
        if channels is None:
            if bps > 0:
                body = ["  no valid RC_CHANNELS_PACKED yet - bytes arriving "
                        "but not decoding (wrong baud? crc_err rising?)", ""]
            else:
                body = ["  no bytes on the wire - check signal->pin10, "
                        "GND->pin6, UART enabled, console off", ""]
        else:
            values = scale_channels(channels) if show_us else list(channels)
            unit = "us " if show_us else "raw"
            body = [
                f"  CH  1-8 [{unit}]: " + " ".join(f"{v:4d}" for v in values[:8]),
                f"  CH 9-16 [{unit}]: " + " ".join(f"{v:4d}" for v in values[8:]),
            ]
        _draw([header, stats] + body, first)
        first = False


def run_raw(ser, port, baud):
    print(f"CRSF raw monitor  {port} @ {baud}   (Ctrl+C to quit)")
    print("periodic hex sample of the incoming stream; look for sync bytes "
          "EE / C8\n")
    window_bytes = 0
    window_start = time.monotonic()
    sample = b""
    while True:
        data = ser.read(256)
        if data:
            window_bytes += len(data)
            sample = data
        now = time.monotonic()
        dt = now - window_start
        if dt < 0.5:
            continue
        bps = window_bytes / dt if dt > 0 else 0.0
        hexs = " ".join(f"{b:02X}" for b in sample[:32])
        sync = sum(1 for b in sample if b in DEFAULT_SYNC_BYTES)
        print(f"{bps:8.0f} bytes/s | sync(EE/C8) in sample: {sync:2d} | {hexs}")
        window_bytes = 0
        window_start = now
        sample = b""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port",
                    default=os.environ.get("CRSF_GW_SERIAL_PORT", "/dev/ttyAMA0"),
                    help="serial device (default: $CRSF_GW_SERIAL_PORT or "
                         "/dev/ttyAMA0)")
    ap.add_argument("-b", "--baud", type=int, default=921600,
                    help="baud, must match the Taranis (default 921600)")
    ap.add_argument("--us", action="store_true",
                    help="show channels in microseconds instead of raw 0..2047")
    ap.add_argument("--raw", action="store_true",
                    help="hex-dump the byte stream instead of decoding")
    args = ap.parse_args()

    ser = open_serial(args.port, args.baud)
    try:
        if args.raw:
            run_raw(ser, args.port, args.baud)
        else:
            run_decoded(ser, args.port, args.baud, args.us)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
