"""Single source of truth for the RPi -> Jetson UDP link packet.

Both sides of the link import this module. See PROTOCOL.md for the
human-readable specification.

Layout (little-endian, fixed 48 bytes):

    offset  field     type        notes
    0       magic     uint16      0x4C43, filters foreign datagrams
    2       version   uint8       protocol version, currently 1
    3       flags     uint8       bit0 = data_valid, rest reserved
    4       seq       uint32      monotonic counter, wraps at 2**32
    8       t_us      uint64      sender monotonic clock, microseconds
    16      channels  uint16[16]  raw CRSF values 0..2047 (NOT microseconds)
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

MAGIC = 0x4C43
VERSION = 1
FLAG_DATA_VALID = 0x01
NUM_CHANNELS = 16
SEQ_MODULO = 1 << 32

_STRUCT = struct.Struct("<HBBIQ16H")
PACKET_SIZE = _STRUCT.size
assert PACKET_SIZE == 48


class LinkProtocolError(ValueError):
    """Raised when a datagram is not a valid link packet."""


@dataclass(frozen=True)
class LinkPacket:
    seq: int
    t_us: int
    channels: tuple  # 16 raw CRSF values, 0..2047
    flags: int = FLAG_DATA_VALID

    @property
    def data_valid(self) -> bool:
        return bool(self.flags & FLAG_DATA_VALID)


def monotonic_us() -> int:
    """Sender timestamp source: monotonic clock in microseconds."""
    return time.monotonic_ns() // 1000


def pack(seq: int, t_us: int, channels, data_valid: bool = True) -> bytes:
    if len(channels) != NUM_CHANNELS:
        raise LinkProtocolError(
            f"expected {NUM_CHANNELS} channels, got {len(channels)}"
        )
    flags = FLAG_DATA_VALID if data_valid else 0
    return _STRUCT.pack(MAGIC, VERSION, flags, seq % SEQ_MODULO, t_us, *channels)


def unpack(data: bytes) -> LinkPacket:
    if len(data) != PACKET_SIZE:
        raise LinkProtocolError(
            f"bad packet size {len(data)}, expected {PACKET_SIZE}"
        )
    magic, version, flags, seq, t_us, *channels = _STRUCT.unpack(data)
    if magic != MAGIC:
        raise LinkProtocolError(f"bad magic 0x{magic:04X}")
    if version != VERSION:
        raise LinkProtocolError(f"unsupported protocol version {version}")
    return LinkPacket(seq=seq, t_us=t_us, channels=tuple(channels), flags=flags)
