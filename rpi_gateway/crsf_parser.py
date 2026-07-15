"""Streaming CRSF frame parser.

Frame format: ``[addr][len][type][payload...][crc8]``

- ``len`` counts everything after itself (type + payload + crc), so the full
  frame is ``len + 2`` bytes.
- ``crc8`` is DVB-S2 (polynomial 0xD5) over type + payload, i.e.
  ``frame[2:-1]``.
- RC data is frame type 0x16 ``RC_CHANNELS_PACKED``: 22-byte payload with
  16 channels of 11 bits each, LSB-first.

The parser never raises on garbage input: bytes are discarded until a
plausible frame validates its CRC; on any mismatch it advances one byte and
resynchronizes.
"""

from __future__ import annotations

from dataclasses import dataclass

CRSF_ADDR_TRANSMITTER = 0xEE  # EdgeTX external-module output
CRSF_ADDR_FLIGHT_CONTROLLER = 0xC8  # CRSF standard sync/address byte
DEFAULT_SYNC_BYTES = (CRSF_ADDR_TRANSMITTER, CRSF_ADDR_FLIGHT_CONTROLLER)

FRAME_TYPE_RC_CHANNELS_PACKED = 0x16
RC_PAYLOAD_LEN = 22
NUM_CHANNELS = 16

MIN_LEN_FIELD = 2  # type + crc
MAX_LEN_FIELD = 62  # CRSF spec maximum


def crc8_dvb_s2(crc: int, byte: int) -> int:
    crc ^= byte
    for _ in range(8):
        if crc & 0x80:
            crc = ((crc << 1) ^ 0xD5) & 0xFF
        else:
            crc = (crc << 1) & 0xFF
    return crc


def crsf_crc(data) -> int:
    """CRC8 DVB-S2 over ``data`` (= type + payload)."""
    crc = 0
    for byte in data:
        crc = crc8_dvb_s2(crc, byte)
    return crc


def unpack_channels(payload) -> list:
    """22-byte RC_CHANNELS_PACKED payload -> list of 16 values, 0..2047."""
    if len(payload) != RC_PAYLOAD_LEN:
        raise ValueError(f"RC payload must be {RC_PAYLOAD_LEN} bytes, got {len(payload)}")
    bits, nbits, channels = 0, 0, []
    for byte in payload:
        bits |= byte << nbits
        nbits += 8
        while nbits >= 11:
            channels.append(bits & 0x7FF)
            bits >>= 11
            nbits -= 11
    return channels[:NUM_CHANNELS]


def pack_channels(channels) -> bytes:
    """Inverse of :func:`unpack_channels`: 16 values -> 22 bytes."""
    if len(channels) != NUM_CHANNELS:
        raise ValueError(f"expected {NUM_CHANNELS} channels, got {len(channels)}")
    bits, nbits, out = 0, 0, bytearray()
    for value in channels:
        bits |= (value & 0x7FF) << nbits
        nbits += 11
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
    return bytes(out)


def build_rc_frame(channels, addr: int = CRSF_ADDR_TRANSMITTER) -> bytes:
    """Build a complete, CRC-valid RC_CHANNELS_PACKED frame (for tests/replay)."""
    body = bytes([FRAME_TYPE_RC_CHANNELS_PACKED]) + pack_channels(channels)
    return bytes([addr, len(body) + 1]) + body + bytes([crsf_crc(body)])


@dataclass(frozen=True)
class CrsfFrame:
    addr: int
    frame_type: int
    payload: bytes


@dataclass
class ParserStats:
    frames_ok: int = 0
    crc_errors: int = 0
    bytes_discarded: int = 0


class CrsfParser:
    """Reassembles CRSF frames from an arbitrary, possibly dirty byte stream."""

    def __init__(self, accept_addresses=DEFAULT_SYNC_BYTES, max_buffer: int = 4096):
        self._accept = frozenset(accept_addresses)
        self._buf = bytearray()
        self._max_buffer = max_buffer
        self.stats = ParserStats()

    def feed(self, data) -> list:
        """Consume ``data``, return every complete valid frame found."""
        self._buf.extend(data)
        frames = []
        while True:
            frame = self._extract_one()
            if frame is None:
                break
            frames.append(frame)
        # Cap the pending buffer so endless garbage cannot grow it unboundedly.
        overflow = len(self._buf) - self._max_buffer
        if overflow > 0:
            del self._buf[:overflow]
            self.stats.bytes_discarded += overflow
        return frames

    def _extract_one(self):
        buf = self._buf
        while True:
            skip = 0
            while skip < len(buf) and buf[skip] not in self._accept:
                skip += 1
            if skip:
                del buf[:skip]
                self.stats.bytes_discarded += skip
            if len(buf) < 2:
                return None
            length = buf[1]
            if not MIN_LEN_FIELD <= length <= MAX_LEN_FIELD:
                del buf[:1]
                self.stats.bytes_discarded += 1
                continue
            full = length + 2
            if len(buf) < full:
                return None  # wait for the rest of the frame
            if crsf_crc(buf[2:full - 1]) != buf[full - 1]:
                self.stats.crc_errors += 1
                del buf[:1]
                self.stats.bytes_discarded += 1
                continue
            frame = CrsfFrame(
                addr=buf[0],
                frame_type=buf[2],
                payload=bytes(buf[3:full - 1]),
            )
            del buf[:full]
            self.stats.frames_ok += 1
            return frame
