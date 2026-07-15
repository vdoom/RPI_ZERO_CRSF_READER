"""Manual SITL check (test plan §8, level 3). Not collected by pytest.

Sends a GCS HEARTBEAT + sweeping RC_CHANNELS_OVERRIDE to ArduPilot SITL and
reads back RC_CHANNELS to verify the FC sees the commanded values on all
16 channels. Then it stops sending so you can observe the GCS failsafe.

Typical SITL session (in another terminal):
    sim_vehicle.py -v ArduCopter --console
    # SITL exposes MAVLink on udp:127.0.0.1:14550 (via MAVProxy --out)

Run:
    python tests/integration/sitl_check.py --conn udpout:127.0.0.1:14550
"""

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil  # noqa: E402

TOLERANCE_US = 2


def sweep_channels(t: float) -> list:
    """Slow triangle sweep, phase-shifted per channel, 1100..1900 us."""
    values = []
    for i in range(16):
        phase = (t * 0.2 + i / 16.0) % 1.0
        tri = 2 * phase if phase < 0.5 else 2 * (1 - phase)
        values.append(int(1100 + tri * 800))
    return values


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conn", default="udpout:127.0.0.1:14550",
                    help="mavutil connection string to SITL")
    ap.add_argument("--duration", type=float, default=15.0,
                    help="seconds to send overrides before stopping")
    ap.add_argument("--source-system", type=int, default=255,
                    help="must equal SYSID_MYGCS on the FC/SITL")
    args = ap.parse_args()

    print(f"connecting to {args.conn} ...")
    conn = mavutil.mavlink_connection(args.conn,
                                      source_system=args.source_system,
                                      source_component=190)
    heartbeat = conn.wait_heartbeat(timeout=20)
    if heartbeat is None:
        print("FAIL: no heartbeat from SITL")
        return 1
    print(f"connected: target system {conn.target_system}, "
          f"component {conn.target_component}")

    t0 = time.monotonic()
    next_heartbeat = 0.0
    next_override = 0.0
    checks = failures = 0
    last_sent = None

    while time.monotonic() - t0 < args.duration:
        now = time.monotonic()
        if now >= next_heartbeat:
            conn.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                    mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                                    0, 0, 0)
            next_heartbeat = now + 1.0
        if now >= next_override:
            last_sent = sweep_channels(now - t0)
            conn.mav.rc_channels_override_send(
                conn.target_system, conn.target_component,
                *last_sent[:8], *last_sent[8:16], 0, 0)
            next_override = now + 0.02  # 50 Hz

        msg = conn.recv_match(type="RC_CHANNELS", blocking=False)
        if msg is not None and last_sent is not None:
            checks += 1
            observed = [getattr(msg, f"chan{i + 1}_raw") for i in range(16)]
            # RC_CHANNELS lags the sweep slightly; allow generous tolerance.
            bad = [i for i in range(16)
                   if abs(observed[i] - last_sent[i]) > 25 + TOLERANCE_US]
            if bad:
                failures += 1
                print(f"  mismatch on channels {bad}: "
                      f"sent={last_sent} observed={observed}")
        time.sleep(0.002)

    print(f"\nRC_CHANNELS snapshots checked: {checks}, mismatching: {failures}")
    if checks == 0:
        print("FAIL: SITL never reported RC_CHANNELS "
              "(check the connection string)")
        return 1
    ratio = failures / checks
    print("PASS" if ratio < 0.2 else "FAIL",
          f"({ratio:.0%} of snapshots outside tolerance; "
          "some lag-induced mismatch is normal)")

    print("\nOverrides and heartbeat STOPPED now.")
    print("Watch the SITL console: with FS_GCS_ENABLE=1 it should report "
          "GCS failsafe / RC override timeout within a few seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
