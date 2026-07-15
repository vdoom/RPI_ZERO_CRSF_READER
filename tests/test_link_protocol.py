import pytest

from protocol import link_protocol

CHANNELS = list(range(100, 100 + 16 * 50, 50))


def test_packet_size_is_48_bytes():
    assert link_protocol.PACKET_SIZE == 48
    packet = link_protocol.pack(0, 0, [0] * 16)
    assert len(packet) == 48


def test_roundtrip():
    packet = link_protocol.pack(12345, 987654321, CHANNELS)
    decoded = link_protocol.unpack(packet)
    assert decoded.seq == 12345
    assert decoded.t_us == 987654321
    assert list(decoded.channels) == CHANNELS
    assert decoded.data_valid


def test_data_valid_flag():
    packet = link_protocol.pack(1, 2, CHANNELS, data_valid=False)
    assert not link_protocol.unpack(packet).data_valid


def test_seq_wraps_at_2_pow_32():
    packet = link_protocol.pack(link_protocol.SEQ_MODULO + 5, 0, CHANNELS)
    assert link_protocol.unpack(packet).seq == 5


def test_bad_magic_rejected():
    packet = bytearray(link_protocol.pack(1, 2, CHANNELS))
    packet[0] ^= 0xFF
    with pytest.raises(link_protocol.LinkProtocolError):
        link_protocol.unpack(bytes(packet))


def test_bad_version_rejected():
    packet = bytearray(link_protocol.pack(1, 2, CHANNELS))
    packet[2] = 99
    with pytest.raises(link_protocol.LinkProtocolError):
        link_protocol.unpack(bytes(packet))


def test_wrong_size_rejected():
    packet = link_protocol.pack(1, 2, CHANNELS)
    with pytest.raises(link_protocol.LinkProtocolError):
        link_protocol.unpack(packet[:-1])
    with pytest.raises(link_protocol.LinkProtocolError):
        link_protocol.unpack(packet + b"\x00")


def test_wrong_channel_count_rejected():
    with pytest.raises(link_protocol.LinkProtocolError):
        link_protocol.pack(1, 2, [992] * 15)
