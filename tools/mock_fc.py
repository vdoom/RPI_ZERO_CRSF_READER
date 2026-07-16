"""Minimal mock FC: emits HEARTBEAT like ArduPilot, prints what arrives.

Level-2 stand-in for SITL. Run the real bridge with
    MAV_BRIDGE_MAVLINK_DEVICE=udpin:0.0.0.0:14551 python3 -m jetson_bridge.bridge
and this script alongside it:
    python3 mock_fc.py --conn udpout:127.0.0.1:14551 --duration 30
"""
import argparse
import collections
import os
import time

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--conn", default="udpout:127.0.0.1:14551")
    ap.add_argument("--duration", type=float, default=30.0)
    args = ap.parse_args()

    conn = mavutil.mavlink_connection(args.conn, source_system=1,
                                      source_component=1)
    t0 = time.monotonic()
    next_hb = 0.0
    n_override = 0
    n_hb = 0
    last = None
    src_sys = None
    stamps = collections.deque(maxlen=5000)

    while time.monotonic() - t0 < args.duration:
        now = time.monotonic()
        if now >= next_hb:
            conn.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                                    mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
                                    0, 0, mavutil.mavlink.MAV_STATE_ACTIVE)
            next_hb = now + 1.0
        msg = conn.recv_match(blocking=True, timeout=0.2)
        if msg is None:
            continue
        mtype = msg.get_type()
        if mtype == "RC_CHANNELS_OVERRIDE":
            n_override += 1
            stamps.append(now)
            last = msg
            src_sys = msg.get_srcSystem()
        elif mtype == "HEARTBEAT":
            n_hb += 1

    print(f"heartbeats from bridge: {n_hb}")
    print(f"overrides received:     {n_override}")
    if len(stamps) >= 2:
        rate = (len(stamps) - 1) / (stamps[-1] - stamps[0])
        print(f"override rate:          {rate:.1f} Hz")
    if last is not None:
        vals = [getattr(last, f"chan{i}_raw") for i in range(1, 19)]
        print(f"last chan1-16_raw:      {vals[:16]}")
        print(f"chan17/18 (expect 0,0): {vals[16:]}")
        print(f"bridge source_system:   {src_sys} (must equal SYSID_MYGCS)")
    ok = n_override > 0 and n_hb > 0
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
