"""Gateway input sources: SPI-sampled CRSF and SBUS end-to-end to channels.

Requires numpy; skipped where it is not installed.
"""

import pytest

np = pytest.importorskip("numpy")

from rpi_gateway.crsf_parser import build_rc_frame  # noqa: E402
from rpi_gateway.crsf_reader import (  # noqa: E402
    DEFAULTS,
    CrsfSpiSource,
    SbusSpiSource,
    load_config,
)
from rpi_gateway.sbus_decoder import (  # noqa: E402
    encode_sbus_frame,
    uart_encode_samples,
)

CHANNELS_A = [172, 992, 1811, 0, 2047, 1000, 1500, 500,
              992, 172, 1811, 992, 700, 1300, 992, 992]


class FakeSampler:
    """Plays back a prepared sample capture in chunks, then idle."""

    def __init__(self, capture: bytes, speed_hz: int, chunk: int = 512,
                 idle_byte: int = 0x00):
        self.speed_hz = speed_hz
        self._capture = capture
        self._pos = 0
        self._chunk = chunk
        self._idle = bytes([idle_byte]) * chunk
        self.closed = False

    def read(self) -> bytes:
        if self._pos >= len(self._capture):
            return self._idle
        chunk = self._capture[self._pos:self._pos + self._chunk]
        self._pos += self._chunk
        return chunk

    def close(self) -> None:
        self.closed = True


def spi_cfg(**overrides):
    cfg = dict(DEFAULTS)
    cfg.update(overrides)
    return cfg


def drain(source, reads=64):
    channels = []
    for _ in range(reads):
        channels.extend(source.poll())
    return channels


def test_crsf_spi_source_decodes_inverted_crsf():
    baud, speed = 400000, 4_000_000
    wire = build_rc_frame(CHANNELS_A) * 3
    capture = uart_encode_samples(wire, speed / baud, inverted=True,
                                  parity=None, stop_bits=1)
    source = CrsfSpiSource(spi_cfg(input_mode="crsf_spi", baud=baud),
                           sampler=FakeSampler(capture, speed))
    got = drain(source)
    assert len(got) == 3
    assert all(ch == CHANNELS_A for ch in got)
    source.close()


def test_sbus_spi_source_decodes_inverted_sbus():
    speed = 1_000_000
    wire = encode_sbus_frame(CHANNELS_A) * 3
    capture = uart_encode_samples(wire, speed / 100000, inverted=True,
                                  parity="even", stop_bits=2)
    source = SbusSpiSource(spi_cfg(input_mode="sbus_spi"),
                           sampler=FakeSampler(capture, speed))
    got = drain(source)
    assert len(got) == 3
    assert all(ch == CHANNELS_A for ch in got)


def test_sbus_spi_source_skips_failsafe_frames():
    speed = 1_000_000
    wire = (encode_sbus_frame(CHANNELS_A, failsafe=True)
            + encode_sbus_frame(CHANNELS_A))
    capture = uart_encode_samples(wire, speed / 100000, inverted=True,
                                  parity="even", stop_bits=2)
    source = SbusSpiSource(spi_cfg(input_mode="sbus_spi"),
                           sampler=FakeSampler(capture, speed))
    got = drain(source)
    assert len(got) == 1  # the failsafe-flagged frame must not be forwarded
    assert source.failsafe_frames == 1


def test_load_config_rejects_bad_input_mode(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("input_mode: telepathy\n")
    with pytest.raises(SystemExit):
        load_config(str(bad))
