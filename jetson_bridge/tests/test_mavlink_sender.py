"""RC_CHANNELS_OVERRIDE construction -> decode -> field check (MAVLink2).

Requires pymavlink; skipped where it is not installed.
"""

import io
import os

import pytest

os.environ.setdefault("MAVLINK20", "1")
mavutil = pytest.importorskip("pymavlink.mavutil")

from jetson_bridge.channel_scaler import scale_channels  # noqa: E402
from jetson_bridge.mavlink_sender import MavlinkError, MavlinkSender  # noqa: E402

CHANNELS_US = [1000 + i * 25 for i in range(16)]


def _roundtrip(msg, tx):
    buf = msg.pack(tx)
    assert buf[0] == 0xFD, "expected a MAVLink2 frame (magic 0xFD)"
    rx = mavutil.mavlink.MAVLink(io.BytesIO())
    rx.robust_parsing = True
    decoded = rx.parse_buffer(bytearray(buf))
    assert decoded and len(decoded) == 1
    return decoded[0]


def test_rc_channels_override_16_channels_mavlink2():
    tx = mavutil.mavlink.MAVLink(io.BytesIO(), srcSystem=255, srcComponent=190)
    msg = tx.rc_channels_override_encode(
        1, 1, *CHANNELS_US[:8], *CHANNELS_US[8:16], 0, 0)
    decoded = _roundtrip(msg, tx)
    assert decoded.get_type() == "RC_CHANNELS_OVERRIDE"
    assert decoded.target_system == 1
    assert decoded.target_component == 1
    for i in range(16):
        assert getattr(decoded, f"chan{i + 1}_raw") == CHANNELS_US[i]
    assert decoded.chan17_raw == 0
    assert decoded.chan18_raw == 0
    assert decoded.get_srcSystem() == 255
    assert decoded.get_srcComponent() == 190


def test_gcs_heartbeat_fields():
    tx = mavutil.mavlink.MAVLink(io.BytesIO(), srcSystem=255, srcComponent=190)
    msg = tx.heartbeat_encode(
        mavutil.mavlink.MAV_TYPE_GCS,
        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
        0, 0, 0)
    decoded = _roundtrip(msg, tx)
    assert decoded.get_type() == "HEARTBEAT"
    assert decoded.type == mavutil.mavlink.MAV_TYPE_GCS
    assert decoded.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID


def test_scaled_channels_fit_override_range():
    for channels in ([0] * 16, [2047] * 16, list(range(0, 2048, 128))):
        us = scale_channels(channels)
        assert len(us) == 16
        assert all(0 <= value <= 65535 for value in us)


def test_sender_rejects_wrong_channel_count():
    sender = MavlinkSender("dummy-device")
    with pytest.raises(MavlinkError):
        sender.send_override([1500] * 15)


def test_sender_requires_connection():
    sender = MavlinkSender("dummy-device")
    assert not sender.connected
    with pytest.raises(MavlinkError):
        sender.send_override([1500] * 16)
    with pytest.raises(MavlinkError):
        sender.send_heartbeat()
