"""Ground-side gateway: read RC channels from the radio, forward over UDP.

Input modes (``input_mode`` in config.yaml):

  crsf_uart  CRSF on the PL011 hardware UART (/dev/ttyAMA0, phys pin 10).
             Only works for NON-inverted CRSF.
  crsf_spi   INVERTED CRSF (as emitted on the Taranis/RadioMaster module-bay
             S.Port pin): the SPI peripheral samples MISO (GPIO9, phys
             pin 21) and the UART waveform is decoded in software.
             Requires numpy and SPI enabled.
  sbus_spi   INVERTED SBUS, same SPI sampling technique.

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

INPUT_MODES = ("crsf_uart", "crsf_spi", "sbus_spi")

DEFAULTS = {
    "input_mode": "crsf_uart",
    "serial_port": "/dev/ttyAMA0",
    "baud": 921600,
    "accept_sync_bytes": list(DEFAULT_SYNC_BYTES),
    "spi_device": "/dev/spidev0.0",
    "spi_speed_hz": 0,  # 0 = auto: 10x baud (crsf_spi) / 1 MHz (sbus_spi)
    "spi_inverted": True,
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
    cfg = _apply_env_overrides(cfg, ENV_PREFIX)
    if cfg["input_mode"] not in INPUT_MODES:
        raise SystemExit(f"input_mode must be one of {INPUT_MODES}")
    return cfg


class CrsfUartSource:
    """CRSF from the hardware UART; reopens the port on errors."""

    def __init__(self, cfg: dict):
        import serial
        self._serial = serial
        self._port = cfg["serial_port"]
        self._baud = int(cfg["baud"])
        self._retry_delay = float(cfg["serial_retry_delay_s"])
        self._parser = CrsfParser(accept_addresses=cfg["accept_sync_bytes"])
        self._ser = None

    def describe(self) -> str:
        return f"CRSF on {self._port} @ {self._baud} baud (hardware UART)"

    def poll(self) -> list:
        if self._ser is None:
            try:
                self._ser = self._serial.Serial(self._port, self._baud,
                                                timeout=0.02)
                log.info("serial port %s opened", self._port)
            except (self._serial.SerialException, OSError) as exc:
                log.error("cannot open %s: %s; retrying in %.1f s",
                          self._port, exc, self._retry_delay)
                time.sleep(self._retry_delay)
                return []
        try:
            data = self._ser.read(256)
        except (self._serial.SerialException, OSError) as exc:
            log.error("serial error: %s; reopening", exc)
            self.close()
            return []
        return _crsf_channels(self._parser, data)

    def stats_line(self) -> str:
        stats = self._parser.stats
        return (f"frames ok {stats.frames_ok}, crc errors {stats.crc_errors}, "
                f"discarded {stats.bytes_discarded} bytes")

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


class _SpiSourceBase:
    """Shared plumbing for SPI-sampled (inverted) inputs."""

    def __init__(self, cfg: dict, baud: int, default_speed: int,
                 parity, stop_bits: int, sampler=None):
        from rpi_gateway.sbus_decoder import SoftUartDecoder, SpiSampler
        speed = int(cfg["spi_speed_hz"]) or default_speed
        self._sampler = sampler or SpiSampler(cfg["spi_device"],
                                              speed_hz=speed)
        self.speed = speed
        self.baud = baud
        self._uart = SoftUartDecoder(speed / baud,
                                     inverted=bool(cfg["spi_inverted"]),
                                     parity=parity, stop_bits=stop_bits)

    def poll(self) -> list:
        return self._extract(self._uart.feed(self._sampler.read()))

    def close(self) -> None:
        self._sampler.close()

    def _uart_stats(self) -> str:
        return (f"parity errors {self._uart.stats.parity_errors}, "
                f"framing errors {self._uart.stats.framing_errors}")


class CrsfSpiSource(_SpiSourceBase):
    """Inverted CRSF via SPI sampling + software UART decode."""

    def __init__(self, cfg: dict, sampler=None):
        baud = int(cfg["baud"])
        super().__init__(cfg, baud=baud, default_speed=baud * 10,
                         parity=None, stop_bits=1, sampler=sampler)
        self._parser = CrsfParser(accept_addresses=cfg["accept_sync_bytes"])

    def describe(self) -> str:
        return (f"CRSF (inverted) via SPI {self.speed} sps, "
                f"baud {self.baud}, software UART")

    def _extract(self, decoded: bytes) -> list:
        return _crsf_channels(self._parser, decoded)

    def stats_line(self) -> str:
        stats = self._parser.stats
        return (f"frames ok {stats.frames_ok}, crc errors {stats.crc_errors}, "
                f"{self._uart_stats()}")


class SbusSpiSource(_SpiSourceBase):
    """Inverted SBUS via SPI sampling + software UART decode."""

    SBUS_BAUD = 100000

    def __init__(self, cfg: dict, sampler=None):
        super().__init__(cfg, baud=self.SBUS_BAUD, default_speed=1_000_000,
                         parity="even", stop_bits=2, sampler=sampler)
        from rpi_gateway.sbus_decoder import SbusFramer
        self._framer = SbusFramer()
        self.failsafe_frames = 0

    def describe(self) -> str:
        return (f"SBUS (inverted) via SPI {self.speed} sps, "
                f"baud {self.baud}, software UART")

    def _extract(self, decoded: bytes) -> list:
        channels = []
        for frame in self._framer.feed(decoded):
            if frame.failsafe:
                # The radio itself signals failsafe - do not forward sticks.
                self.failsafe_frames += 1
                continue
            channels.append(list(frame.channels))
        return channels

    def stats_line(self) -> str:
        return (f"frames ok {self._framer.stats.frames_ok}, "
                f"resync {self._framer.stats.resync_bytes} bytes, "
                f"failsafe-skipped {self.failsafe_frames}, "
                f"{self._uart_stats()}")


def _crsf_channels(parser: CrsfParser, data: bytes) -> list:
    channels = []
    if data:
        for frame in parser.feed(data):
            if (frame.frame_type == FRAME_TYPE_RC_CHANNELS_PACKED
                    and len(frame.payload) == RC_PAYLOAD_LEN):
                channels.append(unpack_channels(frame.payload))
    return channels


def make_source(cfg: dict):
    mode = cfg["input_mode"]
    if mode == "crsf_uart":
        return CrsfUartSource(cfg)
    if mode == "crsf_spi":
        return CrsfSpiSource(cfg)
    return SbusSpiSource(cfg)


def run(cfg: dict) -> None:
    target = (cfg["udp_target_ip"], int(cfg["udp_target_port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source = make_source(cfg)
    stats_interval = float(cfg["stats_interval_s"])

    log.info("input: %s -> udp://%s:%d", source.describe(),
             target[0], target[1])

    seq = 0
    sent = 0
    last_stats_t = time.monotonic()
    last_stats_sent = 0
    try:
        while True:
            for channels in source.poll():
                packet = link_protocol.pack(
                    seq, link_protocol.monotonic_us(), channels)
                sock.sendto(packet, target)
                seq = (seq + 1) % link_protocol.SEQ_MODULO
                sent += 1

            now = time.monotonic()
            if now - last_stats_t >= stats_interval:
                rate = (sent - last_stats_sent) / (now - last_stats_t)
                log.info("tx %.1f pkt/s (total %d) | %s",
                         rate, sent, source.stats_line())
                last_stats_t = now
                last_stats_sent = sent
    finally:
        source.close()


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
