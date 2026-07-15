import random

import pytest

from rpi_gateway.crsf_parser import (
    CRSF_ADDR_FLIGHT_CONTROLLER,
    CRSF_ADDR_TRANSMITTER,
    FRAME_TYPE_RC_CHANNELS_PACKED,
    CrsfParser,
    build_rc_frame,
    crsf_crc,
    pack_channels,
    unpack_channels,
)

CHANNELS_A = [172, 992, 1811, 0, 2047, 1000, 1500, 500,
              992, 172, 1811, 992, 700, 1300, 992, 992]


def test_crc8_known_answers():
    # Hand-computed: crc(0x00) stays 0; crc(0x01) shifts 0x01 up to bit 7,
    # then (0x80 << 1) ^ 0x1D5 & 0xFF == 0xD5.
    assert crsf_crc(b"\x00") == 0x00
    assert crsf_crc(b"\x01") == 0xD5
    assert crsf_crc(b"") == 0x00


def test_unpack_known_byte_patterns():
    assert unpack_channels(b"\x00" * 22) == [0] * 16
    assert unpack_channels(b"\xff" * 22) == [2047] * 16


def test_unpack_rejects_bad_length():
    with pytest.raises(ValueError):
        unpack_channels(b"\x00" * 21)


def test_pack_unpack_roundtrip():
    rng = random.Random(42)
    for _ in range(200):
        channels = [rng.randrange(0, 2048) for _ in range(16)]
        packed = pack_channels(channels)
        assert len(packed) == 22
        assert unpack_channels(packed) == channels


def test_parser_single_frame():
    parser = CrsfParser()
    frames = parser.feed(build_rc_frame(CHANNELS_A))
    assert len(frames) == 1
    frame = frames[0]
    assert frame.addr == CRSF_ADDR_TRANSMITTER
    assert frame.frame_type == FRAME_TYPE_RC_CHANNELS_PACKED
    assert unpack_channels(frame.payload) == CHANNELS_A
    assert parser.stats.frames_ok == 1
    assert parser.stats.crc_errors == 0


def test_parser_accepts_both_sync_addresses():
    parser = CrsfParser()
    wire = (build_rc_frame(CHANNELS_A, addr=CRSF_ADDR_TRANSMITTER)
            + build_rc_frame(CHANNELS_A, addr=CRSF_ADDR_FLIGHT_CONTROLLER))
    frames = parser.feed(wire)
    assert [f.addr for f in frames] == [CRSF_ADDR_TRANSMITTER,
                                        CRSF_ADDR_FLIGHT_CONTROLLER]


def test_parser_byte_by_byte_delivery():
    parser = CrsfParser()
    frames = []
    for byte in build_rc_frame(CHANNELS_A):
        frames.extend(parser.feed(bytes([byte])))
    assert len(frames) == 1
    assert unpack_channels(frames[0].payload) == CHANNELS_A


def test_parser_multiple_frames_in_one_feed():
    parser = CrsfParser()
    channel_sets = [[i * 100 % 2048] * 16 for i in range(5)]
    wire = b"".join(build_rc_frame(ch) for ch in channel_sets)
    frames = parser.feed(wire)
    assert [unpack_channels(f.payload) for f in frames] == channel_sets


def test_parser_garbage_prefix_then_frame():
    parser = CrsfParser()
    garbage = bytes([0x12, 0x34, 0x56, 0x78]) * 10
    frames = parser.feed(garbage + build_rc_frame(CHANNELS_A))
    assert len(frames) == 1
    assert unpack_channels(frames[0].payload) == CHANNELS_A
    assert parser.stats.bytes_discarded >= len(garbage)


def test_parser_bad_crc_then_resync():
    parser = CrsfParser()
    good = build_rc_frame(CHANNELS_A)
    corrupted = good[:-1] + bytes([good[-1] ^ 0xFF])
    frames = parser.feed(corrupted + good)
    assert len(frames) == 1
    assert unpack_channels(frames[0].payload) == CHANNELS_A
    assert parser.stats.crc_errors >= 1


def test_parser_frame_split_across_reads_with_garbage_between_frames():
    parser = CrsfParser()
    good = build_rc_frame(CHANNELS_A)
    stream = good[:10]
    frames = list(parser.feed(stream))
    assert frames == []
    frames = parser.feed(good[10:] + b"\x00\x00\x00" + good)
    assert len(frames) == 2
    for frame in frames:
        assert unpack_channels(frame.payload) == CHANNELS_A


def test_parser_passes_through_other_frame_types():
    # e.g. LINK_STATISTICS (0x14); the reader filters, the parser does not.
    body = bytes([0x14]) + bytes(range(10))
    wire = bytes([CRSF_ADDR_FLIGHT_CONTROLLER, len(body) + 1]) + body \
        + bytes([crsf_crc(body)])
    frames = CrsfParser().feed(wire)
    assert len(frames) == 1
    assert frames[0].frame_type == 0x14
    assert frames[0].payload == bytes(range(10))


def test_parser_never_crashes_on_random_garbage():
    parser = CrsfParser()
    rng = random.Random(1234)
    garbage = bytes(rng.randrange(256) for _ in range(20000))
    for i in range(0, len(garbage), 37):
        parser.feed(garbage[i:i + 37])
    # After arbitrary garbage the parser may be waiting on a bogus partial
    # frame; padding gives it room to resync onto the real frame.
    frames = parser.feed(build_rc_frame(CHANNELS_A) + b"\x00" * 80)
    rc_frames = [f for f in frames
                 if f.frame_type == FRAME_TYPE_RC_CHANNELS_PACKED
                 and unpack_channels(f.payload) == CHANNELS_A]
    assert len(rc_frames) >= 1


def test_parser_buffer_stays_bounded():
    parser = CrsfParser(max_buffer=1024)
    # 0xC8 followed by len=62 keeps promising a frame that never completes.
    for _ in range(100):
        parser.feed(bytes([0xC8, 62]) + b"\xc8" * 100)
    assert len(parser._buf) <= 1024
