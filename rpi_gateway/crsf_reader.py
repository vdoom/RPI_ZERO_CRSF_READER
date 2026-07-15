"""Ground-side gateway: read CRSF from UART, forward RC channels over UDP.

Run as ``python3 -m rpi_gateway.crsf_reader --config rpi_gateway/config.yaml``
from the repo root (the systemd unit does exactly that).
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time

import serial
import yaml

from protocol import link_protocol
from rpi_gateway.crsf_parser import (
    DEFAULT_SYNC_BYTES,
    FRAME_TYPE_RC_CHANNELS_PACKED,
    RC_PAYLOAD_LEN,
    CrsfParser,
    unpack_channels,
)

log = logging.getLogger("crsf_gateway")

DEFAULTS = {
    "serial_port": "/dev/ttyAMA0",
    "baud": 921600,
    "accept_sync_bytes": list(DEFAULT_SYNC_BYTES),
    "udp_target_ip": "192.168.1.20",
    "udp_target_port": 14650,
    "log_level": "INFO",
    "stats_interval_s": 5.0,
    "serial_retry_delay_s": 2.0,
}

ENV_PREFIX = "CRSF_GW_"


def _apply_env_overrides(cfg: dict, prefix: str) -> dict:
    for key, default in list(cfg.items()):
        raw = os.environ.get(prefix + key.upper())
        if raw is None:
            continue
        if isinstance(default, bool):
            cfg[key] = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            cfg[key] = int(raw, 0)
        elif isinstance(default, float):
            cfg[key] = float(raw)
        elif isinstance(default, list):
            cfg[key] = [int(item, 0) for item in raw.split(",")]
        else:
            cfg[key] = raw
    return cfg


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if path is None:
        candidate = os.path.join(os.path.dirname(__file__), "config.yaml")
        path = candidate if os.path.exists(candidate) else None
    if path is not None:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        unknown = set(loaded) - set(cfg)
        if unknown:
            raise SystemExit(f"unknown config keys in {path}: {sorted(unknown)}")
        cfg.update(loaded)
    return _apply_env_overrides(cfg, ENV_PREFIX)


def run(cfg: dict) -> None:
    target = (cfg["udp_target_ip"], int(cfg["udp_target_port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    parser = CrsfParser(accept_addresses=cfg["accept_sync_bytes"])
    stats_interval = float(cfg["stats_interval_s"])
    retry_delay = float(cfg["serial_retry_delay_s"])

    seq = 0
    sent = 0
    last_stats_t = time.monotonic()
    last_stats_sent = 0

    while True:
        try:
            ser = serial.Serial(cfg["serial_port"], int(cfg["baud"]), timeout=0.02)
        except (serial.SerialException, OSError) as exc:
            log.error("cannot open %s: %s; retrying in %.1f s",
                      cfg["serial_port"], exc, retry_delay)
            time.sleep(retry_delay)
            continue

        log.info("reading CRSF from %s @ %d baud -> udp://%s:%d",
                 cfg["serial_port"], int(cfg["baud"]), target[0], target[1])
        try:
            with ser:
                while True:
                    data = ser.read(256)
                    if data:
                        for frame in parser.feed(data):
                            if frame.frame_type != FRAME_TYPE_RC_CHANNELS_PACKED:
                                continue
                            if len(frame.payload) != RC_PAYLOAD_LEN:
                                continue
                            channels = unpack_channels(frame.payload)
                            packet = link_protocol.pack(
                                seq, link_protocol.monotonic_us(), channels)
                            sock.sendto(packet, target)
                            seq = (seq + 1) % link_protocol.SEQ_MODULO
                            sent += 1

                    now = time.monotonic()
                    if now - last_stats_t >= stats_interval:
                        rate = (sent - last_stats_sent) / (now - last_stats_t)
                        log.info(
                            "tx %.1f pkt/s (total %d), frames ok %d, "
                            "crc errors %d, discarded %d bytes",
                            rate, sent, parser.stats.frames_ok,
                            parser.stats.crc_errors,
                            parser.stats.bytes_discarded)
                        last_stats_t = now
                        last_stats_sent = sent
        except (serial.SerialException, OSError) as exc:
            log.error("serial error: %s; reopening in %.1f s", exc, retry_delay)
            time.sleep(retry_delay)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="path to config.yaml "
                    "(default: rpi_gateway/config.yaml if present)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
