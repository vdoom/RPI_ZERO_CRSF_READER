"""Live SBUS monitor over SPI sampling - for INVERTED signals the Pi's UART
cannot read (SBUS, S.Port).

Wiring: signal -> physical pin 21 (GPIO9 / SPI0 MISO), GND -> pin 6.
Requires SPI enabled (dtparam=spi=on) and numpy. Read-only: no UDP, no FC.

The SPI peripheral samples the pin at --speed Hz; the waveform is decoded
in software (see rpi_gateway/sbus_decoder.py).

Usage:
  python3 tools/sbus_monitor.py            # decode SBUS (100000 baud 8E2 inverted)
  python3 tools/sbus_monitor.py --us       # channels in microseconds
  python3 tools/sbus_monitor.py --scan     # identify what the pin speaks:
                                           # SBUS / CRSF / S.Port telemetry

Reading the live display:
  activity ~0.0%           -> line is flat: wiring/pin problem
  activity >0, frames 0    -> signal present but not SBUS at this baud;
                              run --scan to identify it
  frames ~70-140/s         -> healthy SBUS; channels move with sticks
"""

import argparse
import collections
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from rpi_gateway.sbus_decoder import (  # noqa: E402
    SBUS_BAUD,
    SbusFramer,
    SoftUartDecoder,
    SpiSampler,
)
from tools.crsf_monitor import _draw, scale_channels  # noqa: E402


def line_activity(chunk: bytes) -> float:
    """Fraction of samples HIGH (inverted SBUS idles low, bursts high)."""
    return float(np.unpackbits(np.frombuffer(chunk, dtype=np.uint8)).mean())


class SbusPipeline:
    """Decoded UART bytes -> SBUS frames -> channels + flag line."""

    name = "SBUS"

    def __init__(self):
        self._framer = SbusFramer()
        self.last_frame = None

    def extract(self, decoded: bytes) -> list:
        found = []
        for frame in self._framer.feed(decoded):
            self.last_frame = frame
            found.append(list(frame.channels))
        return found

    def error_line(self) -> str:
        return f"resync:{self._framer.stats.resync_bytes}"

    def extra_lines(self) -> list:
        if self.last_frame is None:
            return []
        f = self.last_frame
        return [f"  flags: frame_lost={f.frame_lost} failsafe={f.failsafe} "
                f"ch17={f.ch17} ch18={f.ch18}"]


class CrsfPipeline:
    """Decoded UART bytes -> CRSF frames -> channels."""

    name = "CRSF"

    def __init__(self):
        from rpi_gateway.crsf_parser import (
            FRAME_TYPE_RC_CHANNELS_PACKED, RC_PAYLOAD_LEN, CrsfParser,
            unpack_channels)
        self._parser = CrsfParser()
        self._rc_type = FRAME_TYPE_RC_CHANNELS_PACKED
        self._rc_len = RC_PAYLOAD_LEN
        self._unpack = unpack_channels

    def extract(self, decoded: bytes) -> list:
        found = []
        for frame in self._parser.feed(decoded):
            if (frame.frame_type == self._rc_type
                    and len(frame.payload) == self._rc_len):
                found.append(self._unpack(frame.payload))
        return found

    def error_line(self) -> str:
        return f"crc_err:{self._parser.stats.crc_errors}"

    def extra_lines(self) -> list:
        return []


def run_live(sampler, args):
    if args.protocol == "crsf":
        uart = SoftUartDecoder(sampler.samples_per_bit(args.baud),
                               inverted=not args.no_invert,
                               parity=None, stop_bits=1)
        pipeline = CrsfPipeline()
    else:
        uart = SoftUartDecoder(sampler.samples_per_bit(args.baud),
                               inverted=not args.no_invert)
        pipeline = SbusPipeline()
    channels = None
    window_frames = 0
    window_bytes = 0
    activity = 0.0
    first = True
    window_start = last_refresh = time.monotonic()

    while True:
        chunk = sampler.read()
        activity = 0.9 * activity + 0.1 * line_activity(chunk)
        decoded = uart.feed(chunk)
        window_bytes += len(decoded)
        for found in pipeline.extract(decoded):
            window_frames += 1
            channels = found

        now = time.monotonic()
        if now - last_refresh < 0.2:
            continue
        dt = now - window_start
        fps = window_frames / dt if dt > 0 else 0.0
        bps = window_bytes / dt if dt > 0 else 0.0
        window_frames = window_bytes = 0
        window_start = last_refresh = now

        header = (f"{pipeline.name} monitor (SPI)  {args.device} @ "
                  f"{args.speed} sps, baud {args.baud}   (Ctrl+C to quit)")
        stats = (f"  frames:{fps:6.1f}/s   bytes:{bps:7.0f}/s   "
                 f"activity:{activity * 100:5.1f}%   "
                 f"parity_err:{uart.stats.parity_errors}   "
                 f"framing_err:{uart.stats.framing_errors}   "
                 f"{pipeline.error_line()}")
        if channels is None:
            if activity < 0.001:
                body = ["  line is flat - check: signal on phys pin 21, "
                        "GND on pin 6, radio ON and transmitting", ""]
            else:
                body = [f"  signal present (activity {activity * 100:.1f}%) "
                        f"but no {pipeline.name} frames - run --scan to "
                        "identify the protocol", ""]
        else:
            values = scale_channels(channels) if args.us else channels
            unit = "us " if args.us else "raw"
            body = [
                f"  CH  1-8 [{unit}]: " + " ".join(f"{v:4d}" for v in values[:8]),
                f"  CH 9-16 [{unit}]: " + " ".join(f"{v:4d}" for v in values[8:]),
            ] + pipeline.extra_lines()
        _draw([header, stats] + body, first)
        first = False


# (label, capture sps, baud, inverted, parity, stop_bits, kind)
SCAN_COMBOS = [
    ("SBUS 100k 8E2 inv",      1_000_000, 100000, True,  "even", 2, "sbus"),
    ("SBUS 100k 8E2 non-inv",  1_000_000, 100000, False, "even", 2, "sbus"),
    ("S.Port 57.6k 8N1 inv",   1_000_000, 57600,  True,  None,   1, "sport"),
    ("CRSF 400k 8N1 non-inv",  4_000_000, 400000, False, None,   1, "crsf"),
    ("CRSF 400k 8N1 inv",      4_000_000, 400000, True,  None,   1, "crsf"),
    ("CRSF 416k 8N1 non-inv",  4_000_000, 416666, False, None,   1, "crsf"),
    ("CRSF 921k 8N1 inv",      4_000_000, 921600, True,  None,   1, "crsf"),
    ("CRSF 921k 8N1 non-inv",  4_000_000, 921600, False, None,   1, "crsf"),
    ("115.2k 8N1 inv",         1_000_000, 115200, True,  None,   1, "other"),
    ("115.2k 8N1 non-inv",     1_000_000, 115200, False, None,   1, "other"),
]


def capture(device, speed, seconds, chunk_bytes=4096):
    sampler = SpiSampler(device, speed_hz=speed, chunk_bytes=chunk_bytes)
    chunks = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chunks.append(sampler.read())
    sampler.close()
    return b"".join(chunks)


def run_scan(args):
    print(f"capturing {args.device} - radio must be ON and transmitting\n")
    captures = {}
    for speed in sorted({c[1] for c in SCAN_COMBOS}):
        captures[speed] = capture(args.device, speed, args.dwell)
        act = line_activity(captures[speed])
        print(f"  captured {args.dwell:.1f}s @ {speed} sps: "
              f"line activity {act * 100:.2f}% high")
    print()
    if all(line_activity(c) < 0.001 for c in captures.values()):
        print("==> Line is flat. Check wiring: signal on phys pin 21 "
              "(GPIO9/MISO), GND on pin 6, radio ON.")
        return

    print(f"{'candidate':<24} | {'bytes':>6} | {'frames':>6} | note")
    print("-" * 70)
    best = None
    from rpi_gateway.crsf_parser import CrsfParser
    for label, speed, baud, inv, parity, stops, kind in SCAN_COMBOS:
        data = captures[speed]
        uart = SoftUartDecoder(speed / baud, inverted=inv,
                               parity=parity, stop_bits=stops)
        decoded = bytearray()
        for i in range(0, len(data), 4096):
            decoded.extend(uart.feed(data[i:i + 4096]))
        note = ""
        frames = 0
        if kind == "sbus":
            framer = SbusFramer()
            frames = sum(len(framer.feed(bytes(decoded[i:i + 4096])))
                         for i in range(0, len(decoded), 4096))
            if frames:
                note = "*** SBUS RC FRAMES ***"
        elif kind == "crsf":
            parser = CrsfParser()
            parser.feed(bytes(decoded))
            frames = parser.stats.frames_ok
            if frames:
                note = "*** CRSF FRAMES ***"
        elif kind == "sport" and decoded:
            common = collections.Counter(decoded).most_common(1)[0]
            share = common[1] / len(decoded)
            if decoded.count(0x7E) / len(decoded) > 0.15:
                note = "looks like S.Port telemetry polls (0x7E) - " \
                       "this pin carries NO RC channels"
            else:
                note = f"top byte 0x{common[0]:02X} ({share:.0%})"
        print(f"{label:<24} | {len(decoded):>6} | {frames:>6} | {note}")
        if frames and (best is None or frames > best[1]):
            best = (label, frames, baud, inv, kind)

    print()
    if best:
        label, frames, baud, inv, kind = best
        print(f"==> {label}: {frames} valid frames.")
        if kind == "sbus":
            invflag = "" if inv else " --no-invert"
            print(f"    Run: python3 tools/sbus_monitor.py{invflag}")
        elif inv:
            print("    This is INVERTED CRSF - the hardware UART cannot "
                  "read it; keep the wire on pin 21 and run:\n"
                  f"    python3 tools/sbus_monitor.py --protocol crsf "
                  f"--baud {baud}")
        else:
            print("    This is non-inverted CRSF - readable by the hardware "
                  "UART; move the wire back to pin 10 and use "
                  f"crsf_monitor.py -b {baud}.")
    else:
        print("==> No candidate decoded. If activity is >0 the signal may "
              "use another baud, or the S.Port hint above applies "
              "(telemetry pin: switch the radio's External RF mode to SBUS).")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="/dev/spidev0.0")
    ap.add_argument("--protocol", choices=["sbus", "crsf"], default="sbus",
                    help="what to decode (default sbus; crsf = inverted "
                         "CRSF via SPI)")
    ap.add_argument("--speed", type=int, default=None,
                    help="SPI sample rate, Hz "
                         "(default: 1000000 for sbus, 4000000 for crsf)")
    ap.add_argument("--baud", type=int, default=None,
                    help="UART baud to decode "
                         "(default: 100000 for sbus, 400000 for crsf)")
    ap.add_argument("--no-invert", action="store_true",
                    help="decode a NON-inverted signal")
    ap.add_argument("--us", action="store_true",
                    help="show channels in microseconds instead of raw")
    ap.add_argument("--scan", action="store_true",
                    help="identify the protocol on the pin, then exit")
    ap.add_argument("--dwell", type=float, default=1.0,
                    help="capture seconds per rate in --scan (default 1.0)")
    args = ap.parse_args()

    if args.speed is None:
        args.speed = 4_000_000 if args.protocol == "crsf" else 1_000_000
    if args.baud is None:
        args.baud = 400000 if args.protocol == "crsf" else SBUS_BAUD

    if args.scan:
        run_scan(args)
        return 0

    sampler = SpiSampler(args.device, speed_hz=args.speed)
    try:
        run_live(sampler, args)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    finally:
        sampler.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
