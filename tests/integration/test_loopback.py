"""Level-2 loopback (test plan §8): synthetic CRSF frames -> parser ->
link packet -> localhost UDP -> receiver -> scaler.

No serial hardware, no FC, no pymavlink required.
"""

import socket

import pytest

# The loopback chain exercises both sides; single-side deploys
# (deploy_rpi.sh / deploy_jetson.sh) stage only one package, so skip there.
pytest.importorskip(
    "jetson_bridge", reason="air-side package not present on this host")
pytest.importorskip(
    "rpi_gateway", reason="ground-side package not present on this host")

from jetson_bridge.channel_scaler import crsf_to_us, scale_channels  # noqa: E402
from jetson_bridge.udp_receiver import UdpReceiver  # noqa: E402
from protocol import link_protocol  # noqa: E402
from rpi_gateway.crsf_parser import (
    FRAME_TYPE_RC_CHANNELS_PACKED,
    CrsfParser,
    build_rc_frame,
    unpack_channels,
)

ANCHOR_SETS = {
    "low": ([172] * 16, [988] * 16),
    "center": ([992] * 16, [1500] * 16),
    "high": ([1811] * 16, [2012] * 16),
}


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


def test_end_to_end_loopback(receiver, tx_sock):
    channel_sets = (
        [ANCHOR_SETS["low"][0], ANCHOR_SETS["center"][0], ANCHOR_SETS["high"][0]]
        + [[172 + i * 100 for i in range(16)]]
    )
    parser = CrsfParser()
    seq = 0
    for channels in channel_sets:
        wire = build_rc_frame(channels)
        # Deliver in awkward 7-byte chunks, as a serial read would.
        frames = []
        for i in range(0, len(wire), 7):
            frames.extend(parser.feed(wire[i:i + 7]))
        assert len(frames) == 1
        assert frames[0].frame_type == FRAME_TYPE_RC_CHANNELS_PACKED
        parsed = unpack_channels(frames[0].payload)
        assert parsed == channels

        packet_bytes = link_protocol.pack(
            seq, link_protocol.monotonic_us(), parsed)
        tx_sock.sendto(packet_bytes, ("127.0.0.1", receiver.port))

        packet = receiver.poll_latest()
        assert packet is not None
        assert packet.seq == seq
        assert list(packet.channels) == channels

        # Same-host latency measurement is meaningful (shared monotonic clock).
        latency_us = link_protocol.monotonic_us() - packet.t_us
        assert 0 <= latency_us < 5_000_000

        assert scale_channels(packet.channels) == [
            max(988, min(2012, crsf_to_us(v))) for v in channels]
        seq += 1

    assert receiver.stats.received == len(channel_sets)
    assert receiver.stats.lost == 0
    assert receiver.stats.invalid == 0


def test_anchor_values_end_to_end(receiver, tx_sock):
    for name, (crsf_values, expected_us) in ANCHOR_SETS.items():
        frames = CrsfParser().feed(build_rc_frame(crsf_values))
        parsed = unpack_channels(frames[0].payload)
        tx_sock.sendto(
            link_protocol.pack(0, link_protocol.monotonic_us(), parsed),
            ("127.0.0.1", receiver.port))
        packet = receiver.poll_latest()
        assert scale_channels(packet.channels) == expected_us, name
