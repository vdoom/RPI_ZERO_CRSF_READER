"""MAVLink2 uplink to the flight controller.

Sends a GCS ``HEARTBEAT`` and ``RC_CHANNELS_OVERRIDE`` with 16 channels.
MAVLink2 is mandatory: channels 9..16 live in extension fields that MAVLink1
does not carry.
"""

from __future__ import annotations

import logging
import os

# Must be set before the first pymavlink import, otherwise MAVLink1 is used
# and channels 9..16 are silently dropped.
os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil  # noqa: E402

log = logging.getLogger("mavlink_sender")

NUM_CHANNELS = 16


class MavlinkError(RuntimeError):
    pass


class MavlinkSender:
    def __init__(self, device: str, baud: int = 921600,
                 source_system: int = 255, source_component: int = 190,
                 heartbeat_timeout_s: float = 10.0):
        self._device = device
        self._baud = baud
        self._source_system = source_system
        self._source_component = source_component
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._conn = None
        self.target_system = 0
        self.target_component = 0

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self) -> None:
        """Open the connection and wait for the FC heartbeat.

        ``source_system`` must equal SYSID_MYGCS on the FC (default 255),
        otherwise ArduPilot silently ignores RC overrides.
        """
        self.close()
        try:
            conn = mavutil.mavlink_connection(
                self._device,
                baud=self._baud,
                source_system=self._source_system,
                source_component=self._source_component,
            )
        except Exception as exc:
            raise MavlinkError(f"cannot open {self._device}: {exc}") from exc

        log.info("waiting for FC heartbeat on %s (timeout %.0f s)",
                 self._device, self._heartbeat_timeout_s)
        heartbeat = conn.wait_heartbeat(timeout=self._heartbeat_timeout_s)
        if heartbeat is None:
            conn.close()
            raise MavlinkError(
                f"no heartbeat from FC on {self._device} "
                f"within {self._heartbeat_timeout_s:.0f} s")
        self.target_system = conn.target_system
        self.target_component = conn.target_component
        self._conn = conn
        log.info("FC connected: target system %d, component %d",
                 self.target_system, self.target_component)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def send_heartbeat(self) -> None:
        conn = self._require_connection()
        try:
            conn.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0, 0, 0)
        except Exception as exc:
            raise MavlinkError(f"heartbeat send failed: {exc}") from exc

    def send_override(self, channels_us) -> None:
        """Send RC_CHANNELS_OVERRIDE with 16 channels (microseconds).

        chan17/chan18 are sent as 0 = "ignore". NOTE: the 0 / UINT16_MAX
        (release) semantics for extension channels 9..18 must be verified
        against the current common.xml and on SITL before relying on them.
        """
        if len(channels_us) != NUM_CHANNELS:
            raise MavlinkError(
                f"expected {NUM_CHANNELS} channels, got {len(channels_us)}")
        conn = self._require_connection()
        try:
            conn.mav.rc_channels_override_send(
                self.target_system, self.target_component,
                *channels_us[:8], *channels_us[8:16], 0, 0)
        except Exception as exc:
            raise MavlinkError(f"override send failed: {exc}") from exc

    def _require_connection(self):
        if self._conn is None:
            raise MavlinkError("not connected to FC")
        return self._conn
