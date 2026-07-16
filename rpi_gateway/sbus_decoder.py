"""Software SBUS decoder: read an INVERTED serial line by sampling it with
the Pi's SPI peripheral (MISO = GPIO9, physical pin 21).

Why: SBUS is UART 100000 baud 8E2 with INVERTED polarity (idle low). The
Pi's PL011 cannot invert RX, and pigpio (bit-bang GPIO serial) is no longer
available on current Raspberry Pi OS. The SPI peripheral, however, is a
perfect fixed-rate line sampler: reading N bytes from /dev/spidev0.0 yields
8*N samples of the MISO pin at the configured SPI clock. We then decode the
UART waveform in software (numpy).

Pipeline:
    SpiSampler.read() -> raw sample bytes
    SoftUartDecoder.feed() -> decoded UART bytes (start/parity/stop checked)
    SbusFramer.feed() -> SbusFrame with 16 channels, 0..2047

SBUS frame (25 bytes): 0x0F | 22 bytes of 16x11-bit channels (LSB-first,
same packing as CRSF) | flags | end byte (0x00, or 0x?4 for S.BUS2 slots).
Flags: bit0 ch17, bit1 ch18, bit2 frame_lost, bit3 failsafe.

SBUS channel values use the same scale as CRSF (172..1811 typical), so the
rest of the pipeline (UDP protocol, Jetson scaler) is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from rpi_gateway.crsf_parser import pack_channels, unpack_channels

SBUS_BAUD = 100000
SBUS_HEADER = 0x0F
SBUS_FRAME_LEN = 25

FLAG_CH17 = 0x01
FLAG_CH18 = 0x02
FLAG_FRAME_LOST = 0x04
FLAG_FAILSAFE = 0x08

# SBUS UART character: 1 start + 8 data + even parity + 2 stop = 12 bits.


# --------------------------------------------------------------------------
# SPI sampler (Linux only; raw ioctl, no python3-spidev dependency)
# --------------------------------------------------------------------------

_SPI_IOC_WR_MODE = 0x40016B01
_SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
_SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04


class SpiSampler:
    """Samples the MISO line (GPIO9 / phys pin 21) at ``speed_hz``."""

    def __init__(self, device: str = "/dev/spidev0.0",
                 speed_hz: int = 1_000_000, chunk_bytes: int = 4096):
        import fcntl
        import os
        import struct
        self._os = os
        self.speed_hz = speed_hz
        self.chunk_bytes = chunk_bytes
        self.fd = os.open(device, os.O_RDWR)
        fcntl.ioctl(self.fd, _SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, _SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, _SPI_IOC_WR_MAX_SPEED_HZ,
                    struct.pack("I", speed_hz))

    def read(self) -> bytes:
        """One SPI transaction; returns chunk_bytes bytes = 8x samples."""
        return self._os.read(self.fd, self.chunk_bytes)

    def samples_per_bit(self, baud: int = SBUS_BAUD) -> float:
        return self.speed_hz / baud

    def close(self) -> None:
        self._os.close(self.fd)


# --------------------------------------------------------------------------
# Soft UART decoder (oversampled bit stream -> bytes)
# --------------------------------------------------------------------------

@dataclass
class UartStats:
    bytes_ok: int = 0
    parity_errors: int = 0
    framing_errors: int = 0  # bad start/stop sample


class SoftUartDecoder:
    """Decode UART characters from an oversampled line capture.

    ``samples_per_bit`` may be fractional. ``inverted=True`` (SBUS/S.Port)
    means the wire idles LOW and a start bit is a HIGH pulse. The frame
    format is configurable so the same decoder can identify SBUS (8E2),
    S.Port and CRSF (8N1) signals.
    """

    def __init__(self, samples_per_bit: float, inverted: bool = True,
                 parity: str | None = "even", stop_bits: int = 2):
        if samples_per_bit < 3:
            raise ValueError("need at least 3 samples per bit")
        if parity not in (None, "even", "odd"):
            raise ValueError("parity must be None, 'even' or 'odd'")
        self.spb = float(samples_per_bit)
        self.inverted = inverted
        self.parity = parity
        self.stop_bits = stop_bits
        self.stats = UartStats()
        self._carry = np.zeros(0, dtype=np.uint8)
        self._nbits = 1 + 8 + (1 if parity else 0) + stop_bits
        # Midpoint sample offsets of the bit periods of one character.
        self._mid = ((np.arange(self._nbits) + 0.5) * self.spb)
        self._char_span = int(math.ceil(self._nbits * self.spb))

    def feed(self, chunk: bytes) -> bytes:
        """Consume raw SPI sample bytes, return decoded UART data bytes."""
        if not chunk:
            return b""
        bits = np.unpackbits(np.frombuffer(chunk, dtype=np.uint8))
        if self.inverted:
            bits = bits ^ 1  # now: idle = 1, start bit = 0 (normal UART view)
        if self._carry.size:
            bits = np.concatenate((self._carry, bits))
        n = bits.size

        out = bytearray()
        # Candidate start bits: falling edges (idle 1 -> start 0).
        edges = np.flatnonzero((bits[:-1] == 1) & (bits[1:] == 0)) + 1
        pos = 0
        carry_from = None
        stop_at = 9 + (1 if self.parity else 0)
        for e in edges:
            e = int(e)
            if e < pos:
                continue
            if e + self._char_span >= n:
                carry_from = e
                break
            sample_idx = (e + self._mid).astype(np.intp)
            char = bits[sample_idx]
            if char[0] != 0 or not bool(char[stop_at:].all()):
                self.stats.framing_errors += 1
                continue
            data = char[1:9]
            if self.parity:
                expected = int(data.sum()) & 1
                if self.parity == "odd":
                    expected ^= 1
                if int(char[9]) != expected:
                    self.stats.parity_errors += 1
                    # Structurally a valid character - skip it whole rather
                    # than resyncing mid-character (which decodes garbage).
                    pos = e + int(self._nbits * self.spb) - 1
                    continue
            out.append(int(np.packbits(data[::-1])[0]))
            self.stats.bytes_ok += 1
            # Next start bit can come right after the stop bits.
            pos = e + int(self._nbits * self.spb) - 1

        if carry_from is not None:
            # Keep one sample *before* the pending start edge, otherwise the
            # 1->0 transition is undetectable in the next chunk and the
            # straddling character would be lost.
            self._carry = bits[max(carry_from - 1, 0):].copy()
        else:
            self._carry = bits[-1:].copy()  # keep 1 sample for edge detection
        return bytes(out)


# --------------------------------------------------------------------------
# SBUS framer (bytes -> frames)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SbusFrame:
    channels: tuple  # 16 values, 0..2047 (same scale as CRSF)
    ch17: bool
    ch18: bool
    frame_lost: bool
    failsafe: bool


@dataclass
class FramerStats:
    frames_ok: int = 0
    resync_bytes: int = 0


def _end_byte_ok(value: int) -> bool:
    # 0x00 = plain SBUS (FrSky/most); 0x04/0x14/0x24/0x34 = S.BUS2 slots.
    return value == 0x00 or (value & 0x0F) == 0x04


class SbusFramer:
    def __init__(self):
        self._buf = bytearray()
        self.stats = FramerStats()

    def feed(self, data: bytes) -> list:
        self._buf.extend(data)
        frames = []
        buf = self._buf
        while len(buf) >= SBUS_FRAME_LEN:
            if buf[0] != SBUS_HEADER or not _end_byte_ok(buf[SBUS_FRAME_LEN - 1]):
                del buf[:1]
                self.stats.resync_bytes += 1
                continue
            channels = unpack_channels(bytes(buf[1:23]))
            flags = buf[23]
            frames.append(SbusFrame(
                channels=tuple(channels),
                ch17=bool(flags & FLAG_CH17),
                ch18=bool(flags & FLAG_CH18),
                frame_lost=bool(flags & FLAG_FRAME_LOST),
                failsafe=bool(flags & FLAG_FAILSAFE),
            ))
            del buf[:SBUS_FRAME_LEN]
            self.stats.frames_ok += 1
        return frames


# --------------------------------------------------------------------------
# Encoding helpers (tests / replay without a radio)
# --------------------------------------------------------------------------

def encode_sbus_frame(channels, ch17=False, ch18=False,
                      frame_lost=False, failsafe=False) -> bytes:
    flags = ((FLAG_CH17 if ch17 else 0) | (FLAG_CH18 if ch18 else 0)
             | (FLAG_FRAME_LOST if frame_lost else 0)
             | (FLAG_FAILSAFE if failsafe else 0))
    return (bytes([SBUS_HEADER]) + pack_channels(channels)
            + bytes([flags, 0x00]))


def uart_encode_samples(data: bytes, samples_per_bit: float,
                        inverted: bool = True, parity: str | None = "even",
                        stop_bits: int = 2, lead_idle_bits: int = 20,
                        gap_bits: int = 2, trail_idle_bits: int = 20) -> bytes:
    """Render bytes as an oversampled UART capture (packed, MSB-first),
    as the SPI sampler would deliver it. For tests and offline replay.
    Defaults match SBUS (8E2 inverted)."""
    levels = []  # UART levels, idle = 1

    def emit(bit, count_bits):
        levels.append((bit, count_bits))

    emit(1, lead_idle_bits)
    for byte in data:
        emit(0, 1)  # start
        ones = 0
        for i in range(8):
            bit = (byte >> i) & 1
            ones += bit
            emit(bit, 1)
        if parity:
            parity_bit = ones & 1
            if parity == "odd":
                parity_bit ^= 1
            emit(parity_bit, 1)
        emit(1, stop_bits)
        if gap_bits:
            emit(1, gap_bits)
    emit(1, trail_idle_bits)

    # Expand to samples with cumulative rounding so fractional rates stay
    # aligned over time, exactly like real hardware.
    samples = []
    t = 0.0
    edge = 0
    for bit, count_bits in levels:
        t += count_bits * samples_per_bit
        n = int(round(t)) - edge
        edge += n
        samples.extend([bit] * n)
    arr = np.array(samples, dtype=np.uint8)
    if inverted:
        arr ^= 1
    # Pad to a whole byte with idle level.
    pad = (-arr.size) % 8
    if pad:
        idle = 0 if inverted else 1
        arr = np.concatenate((arr, np.full(pad, idle, dtype=np.uint8)))
    return np.packbits(arr).tobytes()
