"""Synthetic CRSF frame generator / raw-capture replayer.

Feeds CRSF RC frames into the gateway without a real Taranis, for test plan
levels 2 and up.

Sinks (choose one):
  --pty            create a pseudo-terminal (Linux/macOS) and print its path;
                   point the gateway's serial_port at that path
  --port DEV       write to an existing serial device (e.g. a USB-UART wired
                   to the Pi's RX pin, or a socat-created virtual port)
  --out-file PATH  write the raw byte stream to a file

Examples:
  python tools/crsf_replay.py --pty --pattern sine
  python tools/crsf_replay.py --port /dev/ttyUSB0 --baud 921600 --pattern sweep
  python tools/crsf_replay.py --out-file capture.bin --duration 5
  python tools/crsf_replay.py --pty --replay capture.bin
"""

import argparse
import math
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rpi_gateway.crsf_parser import (  # noqa: E402
    CRSF_ADDR_TRANSMITTER,
    build_rc_frame,
)

CRSF_LOW, CRSF_CENTER, CRSF_HIGH = 172, 992, 1811


def generate_channels(pattern: str, t: float) -> list:
    if pattern == "center":
        return [CRSF_CENTER] * 16
    if pattern == "sweep":
        # Triangle 172..1811 on all channels, phase-shifted per channel,
        # full period 4 s - good for the Mission Planner calibration screen.
        values = []
        for i in range(16):
            phase = (t / 4.0 + i / 16.0) % 1.0
            tri = 2 * phase if phase < 0.5 else 2 * (1 - phase)
            values.append(int(CRSF_LOW + tri * (CRSF_HIGH - CRSF_LOW)))
        return values
    if pattern == "sine":
        values = []
        for i in range(16):
            v = CRSF_CENTER + 819 * math.sin(2 * math.pi * (0.25 * t + i / 16))
            values.append(max(CRSF_LOW, min(CRSF_HIGH, int(round(v)))))
        return values
    raise ValueError(f"unknown pattern: {pattern}")


class _FdSink:
    def __init__(self, fd, name):
        self.fd = fd
        self.name = name

    def write(self, data):
        os.write(self.fd, data)

    def close(self):
        os.close(self.fd)


def open_sink(args):
    chosen = [bool(args.pty), bool(args.port), bool(args.out_file)]
    if sum(chosen) != 1:
        raise SystemExit("choose exactly one sink: --pty, --port or --out-file")
    if args.pty:
        if os.name != "posix":
            raise SystemExit("--pty requires Linux/macOS; on Windows use "
                             "--out-file or a real/virtual COM port")
        import pty
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        print(f"PTY created: {slave_name}")
        print(f"  point the gateway at it, e.g.:")
        print(f"  CRSF_GW_SERIAL_PORT={slave_name} python3 -m rpi_gateway.crsf_reader")
        return _FdSink(master_fd, slave_name)
    if args.port:
        import serial
        return serial.Serial(args.port, args.baud)
    handle = open(args.out_file, "wb")
    handle.name_hint = args.out_file
    return handle


def run_replay(sink, args):
    data = pathlib.Path(args.replay).read_bytes()
    chunk = 64
    delay = chunk * 10.0 / args.baud  # 8N1 = 10 bits per byte on the wire
    sent = 0
    print(f"replaying {len(data)} bytes from {args.replay} "
          f"at ~{args.baud} baud pacing")
    for i in range(0, len(data), chunk):
        sink.write(data[i:i + chunk])
        sent += len(data[i:i + chunk])
        time.sleep(delay)
    print(f"done, {sent} bytes replayed")


def run_generator(sink, args):
    period = 1.0 / args.rate
    t0 = time.monotonic()
    next_tick = t0
    frames = 0
    print(f"generating '{args.pattern}' frames at {args.rate:.0f} Hz "
          f"(addr 0x{args.addr:02X})"
          + (f" for {args.duration:.0f} s" if args.duration else
             ", Ctrl+C to stop"))
    while True:
        now = time.monotonic()
        t = now - t0
        if args.duration and t >= args.duration:
            break
        sink.write(build_rc_frame(generate_channels(args.pattern, t),
                                  addr=args.addr))
        frames += 1
        next_tick += period
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_tick = time.monotonic()
    print(f"done, {frames} frames written")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sink_group = ap.add_argument_group("sink")
    sink_group.add_argument("--pty", action="store_true",
                            help="create a pty and print its path (POSIX)")
    sink_group.add_argument("--port", help="existing serial device to write to")
    sink_group.add_argument("--out-file", help="write raw stream to a file")
    ap.add_argument("--baud", type=int, default=921600,
                    help="baud for --port and replay pacing (default 921600)")
    ap.add_argument("--pattern", choices=["center", "sweep", "sine"],
                    default="sweep")
    ap.add_argument("--rate", type=float, default=50.0,
                    help="frames per second (default 50)")
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds to run (default: forever; 5 s for --out-file)")
    ap.add_argument("--addr", type=lambda s: int(s, 0),
                    default=CRSF_ADDR_TRANSMITTER,
                    help="CRSF address/sync byte (default 0xEE)")
    ap.add_argument("--replay", help="replay a raw capture file instead of "
                    "generating frames")
    args = ap.parse_args()

    if args.out_file and args.duration is None:
        args.duration = 5.0

    sink = open_sink(args)
    try:
        if args.replay:
            run_replay(sink, args)
        else:
            run_generator(sink, args)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
