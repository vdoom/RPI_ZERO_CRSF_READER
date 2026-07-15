import socket

import pytest

from jetson_bridge.udp_receiver import UdpReceiver
from protocol import link_protocol

CHANNELS = [992] * 16


@pytest.fixture
def receiver():
    rx = UdpReceiver("127.0.0.1", 0, timeout_s=0.5)
    yield rx
    rx.close()


@pytest.fixture
def tx_sock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    yield sock
    sock.close()


def send(sock, port, seq, channels=CHANNELS, valid=True):
    packet = link_protocol.pack(seq, link_protocol.monotonic_us(), channels,
                                data_valid=valid)
    sock.sendto(packet, ("127.0.0.1", port))


def test_roundtrip(receiver, tx_sock):
    send(tx_sock, receiver.port, seq=7, channels=list(range(16)))
    packet = receiver.poll_latest()
    assert packet is not None
    assert packet.seq == 7
    assert list(packet.channels) == list(range(16))
    assert packet.data_valid


def test_poll_latest_returns_newest(receiver, tx_sock):
    for seq in range(5):
        send(tx_sock, receiver.port, seq=seq, channels=[seq] * 16)
    packet = receiver.poll_latest()
    assert packet.seq == 4
    assert receiver.stats.received == 5
    assert receiver.stats.lost == 0


def test_loss_detection(receiver, tx_sock):
    for seq in (0, 1, 5):
        send(tx_sock, receiver.port, seq=seq)
    receiver.poll_latest()
    assert receiver.stats.lost == 3


def test_seq_wraparound_is_not_loss(receiver, tx_sock):
    send(tx_sock, receiver.port, seq=link_protocol.SEQ_MODULO - 1)
    send(tx_sock, receiver.port, seq=0)
    receiver.poll_latest()
    assert receiver.stats.lost == 0
    assert receiver.stats.out_of_order == 0


def test_out_of_order_detection(receiver, tx_sock):
    send(tx_sock, receiver.port, seq=10)
    send(tx_sock, receiver.port, seq=8)
    send(tx_sock, receiver.port, seq=11)
    receiver.poll_latest()
    assert receiver.stats.out_of_order == 1
    assert receiver.stats.lost == 0


def test_invalid_datagrams_counted_and_skipped(receiver, tx_sock):
    tx_sock.sendto(b"this is not a link packet", ("127.0.0.1", receiver.port))
    tx_sock.sendto(b"\x00" * 48, ("127.0.0.1", receiver.port))  # bad magic
    send(tx_sock, receiver.port, seq=1)
    packet = receiver.poll_latest()
    assert packet is not None and packet.seq == 1
    assert receiver.stats.invalid == 2
    assert receiver.stats.received == 1


def test_timeout_returns_none():
    rx = UdpReceiver("127.0.0.1", 0, timeout_s=0.05)
    try:
        assert rx.poll_latest() is None
    finally:
        rx.close()


def test_data_valid_flag_roundtrip(receiver, tx_sock):
    send(tx_sock, receiver.port, seq=1, valid=False)
    packet = receiver.poll_latest()
    assert packet is not None
    assert not packet.data_valid
