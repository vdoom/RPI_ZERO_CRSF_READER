"""Air-side bridge: UDP link packets -> MAVLink2 RC_CHANNELS_OVERRIDE.

Failsafe design (the important part):

The bridge sends a GCS HEARTBEAT *only while the end-to-end pilot link is
alive*. If any link in the chain dies - Taranis->RPi (no CRSF frames),
RPi->Jetson (no UDP), or this process itself - the heartbeat stops and the
FC enters GCS failsafe. ``RC_OVERRIDE_TIME`` on the FC is the secondary
backstop for stale overrides.

Watchdog: if no fresh valid UDP packet arrives within ``link_timeout_ms``,
stop sending both HEARTBEAT and RC_CHANNELS_OVERRIDE; resume when the
stream recovers.

Run as ``python3 -m jetson_bridge.bridge --config jetson_bridge/config.yaml``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import yaml

from jetson_bridge.channel_scaler import scale_channels
from jetson_bridge.mavlink_sender import MavlinkError, MavlinkSender
from jetson_bridge.udp_receiver import UdpReceiver

log = logging.getLogger("mavlink_bridge")

DEFAULTS = {
    "udp_listen_ip": "0.0.0.0",
    "udp_listen_port": 14650,
    "mavlink_device": "/dev/ttyTHS1",
    "mavlink_baud": 921600,
    "source_system": 255,      # must equal SYSID_MYGCS on the FC
    "source_component": 190,
    "num_channels": 16,
    "override_rate_hz": 50.0,
    "heartbeat_rate_hz": 1.0,
    "link_timeout_ms": 500,
    "us_min": 988,
    "us_max": 2012,
    "log_level": "INFO",
    "stats_interval_s": 5.0,
    "fc_connect_timeout_s": 10.0,
    "fc_retry_delay_s": 2.0,
}

ENV_PREFIX = "MAV_BRIDGE_"


def _apply_env_overrides(cfg: dict, prefix: str) -> dict:
    for key, default in list(cfg.items()):
        raw = os.environ.get(prefix + key.upper())
        if raw is None:
            continue
        if isinstance(default, bool):
            cfg[key] = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            cfg[key] = int(raw, 0)
        elif isinstance(default, float):
            cfg[key] = float(raw)
        else:
            cfg[key] = raw
    return cfg


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if path is None:
        candidate = os.path.join(os.path.dirname(__file__), "config.yaml")
        path = candidate if os.path.exists(candidate) else None
    if path is not None:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        unknown = set(loaded) - set(cfg)
        if unknown:
            raise SystemExit(f"unknown config keys in {path}: {sorted(unknown)}")
        cfg.update(loaded)
    cfg = _apply_env_overrides(cfg, ENV_PREFIX)
    if int(cfg["num_channels"]) != 16:
        raise SystemExit("num_channels must be 16 (fixed by the link protocol)")
    return cfg


def run(cfg: dict) -> None:
    receiver = UdpReceiver(cfg["udp_listen_ip"], int(cfg["udp_listen_port"]),
                           timeout_s=0.02)
    sender = MavlinkSender(
        cfg["mavlink_device"],
        baud=int(cfg["mavlink_baud"]),
        source_system=int(cfg["source_system"]),
        source_component=int(cfg["source_component"]),
        heartbeat_timeout_s=float(cfg["fc_connect_timeout_s"]),
    )

    timeout_s = float(cfg["link_timeout_ms"]) / 1000.0
    override_period = 1.0 / float(cfg["override_rate_hz"])
    heartbeat_period = 1.0 / float(cfg["heartbeat_rate_hz"])
    stats_interval = float(cfg["stats_interval_s"])
    retry_delay = float(cfg["fc_retry_delay_s"])
    us_min, us_max = int(cfg["us_min"]), int(cfg["us_max"])

    log.info("listening on udp://%s:%d, FC on %s @ %d, "
             "override %.0f Hz, watchdog %d ms",
             cfg["udp_listen_ip"], receiver.port, cfg["mavlink_device"],
             int(cfg["mavlink_baud"]), float(cfg["override_rate_hz"]),
             int(cfg["link_timeout_ms"]))

    channels_us = None
    last_valid_rx = None
    link_ok = False
    next_override = 0.0
    next_heartbeat = 0.0
    overrides_sent = 0
    next_stats = time.monotonic() + stats_interval

    while True:
        if not sender.connected:
            try:
                sender.connect()
            except MavlinkError as exc:
                log.error("FC connect failed: %s; retrying in %.1f s",
                          exc, retry_delay)
                time.sleep(retry_delay)

        packet = receiver.poll_latest()
        now = time.monotonic()
        if packet is not None and packet.data_valid:
            channels_us = scale_channels(packet.channels, us_min, us_max)
            last_valid_rx = now

        fresh = last_valid_rx is not None and (now - last_valid_rx) <= timeout_s
        if fresh and not link_ok:
            log.warning("link UP - resuming HEARTBEAT and RC_CHANNELS_OVERRIDE")
            next_override = next_heartbeat = now
        elif link_ok and not fresh:
            log.warning("link LOST (no valid packet for %d ms) - "
                        "stopping HEARTBEAT and overrides -> FC GCS failsafe",
                        int(cfg["link_timeout_ms"]))
        link_ok = fresh

        if link_ok and sender.connected and channels_us is not None:
            try:
                if now >= next_override:
                    sender.send_override(channels_us)
                    overrides_sent += 1
                    next_override += override_period
                    if next_override < now:  # fell behind: don't burst
                        next_override = now
                if now >= next_heartbeat:
                    sender.send_heartbeat()
                    next_heartbeat += heartbeat_period
                    if next_heartbeat < now:
                        next_heartbeat = now
            except MavlinkError as exc:
                log.error("MAVLink send failed: %s - reconnecting to FC", exc)
                sender.close()

        if now >= next_stats:
            stats = receiver.stats
            log.info("link %s | rx %d, lost %d, out-of-order %d, invalid %d "
                     "| overrides sent %d",
                     "UP" if link_ok else "DOWN", stats.received, stats.lost,
                     stats.out_of_order, stats.invalid, overrides_sent)
            next_stats = now + stats_interval


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="path to config.yaml "
                    "(default: jetson_bridge/config.yaml if present)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
