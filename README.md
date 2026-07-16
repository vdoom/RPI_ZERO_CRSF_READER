# CRSF → WiFi → MAVLink RC bridge

Carries RC control from a FrSky Taranis X9D+ to an ArduPilot flight
controller over a regular WiFi network — no classic RC receiver on board.
**16 channels end-to-end** (hard requirement), MAVLink 2 only.

```
[Taranis X9D+]                [RPi Zero 2 W]              [Jetson Orin Nano]           [FC / ArduPilot]
  External RF = CRSF  --SPI--->  crsf_gateway   --UDP/-->    mavlink_bridge   --MAVLink2-->  RC input
  (module bay, inverted) 3.3V    (decode 16ch)    WiFi       (scale + override)   serial
```

Full specification: [project-passport.md](project-passport.md).
UDP wire format: [protocol/PROTOCOL.md](protocol/PROTOCOL.md).

## Repo layout

| Path | Contents |
|---|---|
| `protocol/` | `link_protocol.py` — single source of truth for the UDP packet + `PROTOCOL.md` |
| `rpi_gateway/` | ground side: CRSF parser, SBUS/soft-UART SPI decoder, reader → UDP sender, `setup_uart.sh`, systemd unit, tests |
| `jetson_bridge/` | air side: UDP receiver, channel scaler, MAVLink sender, watchdog bridge, systemd unit, tests |
| `tools/` | `crsf_monitor.py` (live view, hardware UART), `sbus_monitor.py` (live view + protocol `--scan`, SPI-sampled inverted signals), `crsf_replay.py` (synthetic CRSF without a radio), `latency_probe.py` (latency/loss), `mock_fc.py` (stand-in FC for live bridge tests) |
| `tests/` | link-protocol unit tests, `integration/` loopback + manual SITL check |
| `deploy/` | `deploy_rpi.sh`, `deploy_jetson.sh` — rsync + install over SSH |

## ⚠️ Safety model (read first)

There is no physical RC receiver, so frame-loss RC failsafe is **not** the
primary mechanism. Instead:

- **Primary failsafe — GCS heartbeat.** The Jetson bridge sends `HEARTBEAT`
  (as a GCS) *only while the end-to-end pilot link is alive*. If any link
  breaks — Taranis→RPi (no CRSF), RPi→Jetson (no UDP), or the bridge dies —
  the heartbeat stops and the FC enters **GCS failsafe**.
- **Watchdog on the Jetson:** no fresh valid UDP packet for
  `link_timeout_ms` (default **500 ms**) → stop sending both `HEARTBEAT`
  and `RC_CHANNELS_OVERRIDE`; resume automatically when the stream recovers.
- **Secondary backstop — `RC_OVERRIDE_TIME`** on the FC: stale overrides
  expire after a few seconds even if something keeps a heartbeat alive.

**Mandatory bench checks before any flight (props removed):**

1. Kill WiFi → FC enters GCS failsafe within the timeout.
2. Cut RPi power → same.
3. Disconnect the signal wire from the Taranis → same.
4. Kill each process (`crsf-gateway`, `mavlink-bridge`) in turn → same.
5. Confirm the FC failsafe *action* (RTL/Land/…) is the one you configured.

> Home WiFi is fine for development and the bench. Real flight requires a
> separate assessment of range, reliability and local regulations — out of
> scope here.

## Hardware wiring

### Taranis/RadioMaster → RPi Zero 2 W

**Measured reality (working setup):** with External RF = **CRSF @ 921600**,
the radio emits **inverted CRSF** on the module bay's bottom **S.Port pin**
(a.k.a. the "S.BUS pin"; idle low, ~0.13 V on a voltmeter). The PPM pin
stays silent. The Pi's hardware UART cannot read an inverted signal, so the
gateway samples the pin with the **SPI peripheral** at 4 MHz and decodes
the UART waveform in software (`input_mode: crsf_spi` — the default). No
level shifter, no inverter hardware needed:

- Radio bay **S.Port pin (bottom)** → RPi **GPIO9 / SPI-MISO (physical pin 21)**
- Radio **GND** → RPi **GND** (physical pin 6)
- ⚠️ The **BATT** contact in the bay is ~7.5–8.3 V — **never** to a GPIO
  (kills the Pi). Verify pins with a multimeter first: BATT reads ~7.5 V,
  GND beeps to chassis, the inverted signal pin reads ~0.1–1 V while
  transmitting.
- The RPi has its **own 5 V USB supply**; only GND is shared with the radio.
- SPI must be enabled: `dtparam=spi=on` in `/boot/firmware/config.txt`.

Alternative modes, same wiring: an *inverted SBUS* source (e.g. a real
SBUS receiver) is decoded with `input_mode: sbus_spi`. If your radio
outputs *non-inverted* CRSF (verify: the signal pin idles at ~3.3 V), the
classic hookup works instead: signal → pin 10, and `input_mode: crsf_uart`
(requires `setup_uart.sh` + reboot).

**Identify what your pin actually speaks** (SBUS / inverted CRSF /
non-inverted CRSF / S.Port telemetry) with one command:

```bash
python3 tools/sbus_monitor.py --scan
```

### Jetson → FC

UART (as configured): Jetson `/dev/ttyTHS1` ↔ FC TELEM port — TX↔RX,
RX↔TX, shared GND. `mavlink_baud` (default **1500000**, because this
bench's FC port is shared with another project that needs 1.5 M) must
match `SERIALx_BAUD`. USB (`/dev/ttyACM0`) also works — just change
`mavlink_device` in `jetson_bridge/config.yaml`.

> The deploy script disables `nvgetty` (the serial console that JetPack
> puts on `/dev/ttyTHS1`).

### Radio (EdgeTX/OpenTX)

Model settings → External RF → Mode **CRSF**, baud **921600** — must match
`baud` in `rpi_gateway/config.yaml` (the verified working setup; the pin
carries it inverted, see the wiring section).

After changing endpoints/trims on the radio, verify full stick travel with
`tools/sbus_monitor.py --protocol crsf --baud 921600`: the Jetson scaler
assumes min/mid/max ≈ **172 / 992 / 1811** (→ 988/1500/2012 µs). Adjust the
radio's endpoints if your values differ noticeably — FC RC calibration
absorbs small offsets.

## Device setup

### RPi Zero 2 W — SPI (default `crsf_spi`, also `sbus_spi`)

Enable the SPI peripheral once — `dtparam=spi=on` in
`/boot/firmware/config.txt` (or `sudo raspi-config` → Interface Options →
SPI) — then reboot.

**Check the radio is actually being read** (turn on the radio first):

```bash
python3 tools/sbus_monitor.py --scan                        # identify what the pin speaks
python3 tools/sbus_monitor.py --protocol crsf --baud 921600 # live 16 channels + rates
python3 tools/sbus_monitor.py --protocol crsf --baud 921600 --us   # in microseconds
```

It's read-only (no UDP, no FC), so you can tell wiring problems from decode
problems. Move the sticks and watch the channel values change, and check
the min/mid/max endpoints (see the Radio section above). Stop the
`crsf-gateway` service first if it is running, since only one reader can
hold the SPI device: `sudo systemctl stop crsf-gateway`.

### RPi Zero 2 W — UART (only for `crsf_uart` mode)

```bash
sudo bash rpi_gateway/setup_uart.sh   # then: sudo reboot
```

Enables PL011 (`enable_uart=1`, `dtoverlay=disable-bt`), removes the serial
console from `cmdline.txt`, disables `hciuart`. After reboot the CRSF input
is `/dev/ttyAMA0`. Check it with `python3 tools/crsf_monitor.py`
(`--us` for microseconds, `--raw` for a hex sample to debug wiring/baud).

### FC / ArduPilot parameters (set by you; verify names for your vehicle/firmware)

| Parameter | Value | Why |
|---|---|---|
| `SERIALx_PROTOCOL` | `2` (MAVLink2) | port wired to the Jetson |
| `SERIALx_BAUD` | `1500` | match `mavlink_baud` (1.5 M as configured) |
| `SYSID_MYGCS` | `255` | must equal `source_system` of the bridge, or overrides are silently ignored |
| `FS_GCS_ENABLE` | `1` (+ chosen action) | **primary failsafe**; pick RTL/Land/… for your vehicle |
| `RC_OVERRIDE_TIME` | e.g. `3` s | backstop for stale overrides |
| `RCx_MIN/MAX/TRIM` | 988 / 2012 / 1500 | or run RC calibration while the bridge streams sticks |
| `ARMING_CHECK` | reviewed consciously | without a physical RX some RC checks complain — understand each one you relax, don't disable wholesale |

## Deploy (over SSH)

```bash
# ground side (--with-uart-setup only for crsf_uart mode; the default
# crsf_spi mode needs SPI enabled instead, see Device setup)
RPI_HOST=pi@192.168.1.10 ./deploy/deploy_rpi.sh [--with-uart-setup] [--run-tests]

# air side
JETSON_HOST=user@192.168.1.20 ./deploy/deploy_jetson.sh [--run-tests]
```

Both scripts rsync the code to `/opt/crsf-link` (override with
`INSTALL_DIR`), install dependencies, install + enable the systemd service,
and never overwrite an already-edited `config.yaml` on the device.

Logs: `journalctl -u crsf-gateway -f` / `journalctl -u mavlink-bridge -f`.

## Configuration

Static IPs are assumed (adjust to your LAN):
`rpi_gateway/config.yaml` → `udp_target_ip` = Jetson's IP;
`jetson_bridge/config.yaml` listens on `0.0.0.0:14650`.

Every key can be overridden per-run with an environment variable:
`CRSF_GW_<KEY>` for the gateway, `MAV_BRIDGE_<KEY>` for the bridge, e.g.
`CRSF_GW_SERIAL_PORT=/dev/pts/5`, `MAV_BRIDGE_MAVLINK_DEVICE=udpout:127.0.0.1:14550`.

Key defaults: input **crsf_spi** (inverted CRSF @ **921600**, SPI-sampled
at 4 MHz), UDP port **14650**, override rate **50 Hz**, heartbeat **1 Hz**,
watchdog **500 ms**, clamp **988..2012 µs**.

## Testing

### Level 1 — unit (no hardware; host, or on-device)

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Covers: CRC8 DVB-S2 known answers, channel pack↔unpack round-trip, parser
resync on garbage/bad CRC/split frames, UDP packet encode↔decode, scaling
anchors (172→988, 992→1500, 1811→2012), seq loss detection,
`RC_CHANNELS_OVERRIDE` build→decode with 16 channels in MAVLink2.

### Level 2 — loopback, no radio and no FC

Host-only chain test (also part of the pytest run):
`tests/integration/test_loopback.py`.

With processes, on one Linux box or across RPi + Jetson over real WiFi:

```bash
# terminal 1: fake radio -> prints a PTY path
python tools/crsf_replay.py --pty --pattern sweep

# terminal 2: gateway reading from the PTY
CRSF_GW_SERIAL_PORT=/dev/pts/N CRSF_GW_UDP_TARGET_IP=<jetson-ip> \
    python3 -m rpi_gateway.crsf_reader

# terminal 3 (on the Jetson, bridge stopped): measure loss + latency
python tools/latency_probe.py tap --port 14650 --duration 30

# clock-independent network RTT between the devices:
python tools/latency_probe.py echo-server --port 14651   # on Jetson
python tools/latency_probe.py rtt --target <jetson-ip>:14651  # on RPi
```

> `t_us`-based absolute latency is only valid when sender and receiver share
> a clock (same host). Across devices use the RTT mode (one-way ≈ RTT/2) or
> the "relative" jitter numbers.

No FC on the bench yet? Run the real bridge against a mock FC on the
Jetson — verifies heartbeat, 50 Hz overrides, 16-channel scaling and
chan17/18 = 0 with live radio input:

```bash
# terminal 1 (Jetson): the bridge, pointed at a local UDP "FC"
MAV_BRIDGE_MAVLINK_DEVICE=udpin:0.0.0.0:14551 python3 -m jetson_bridge.bridge
# terminal 2 (Jetson): mock FC - sends HEARTBEAT, reports what it receives
python3 tools/mock_fc.py --conn udpout:127.0.0.1:14551 --duration 30
```

### Level 3 — ArduPilot SITL

```bash
sim_vehicle.py -v ArduCopter --console          # SITL on udp:127.0.0.1:14550
python tests/integration/sitl_check.py --conn udpout:127.0.0.1:14550
```

Verifies the FC sees all 16 commanded channels via `RC_CHANNELS`, then stops
sending so you can watch the GCS failsafe fire. The bridge itself can also be
pointed at SITL: `MAV_BRIDGE_MAVLINK_DEVICE=udpout:127.0.0.1:14550 python3 -m jetson_bridge.bridge`.

### Level 4 — bench with real hardware (manual, props removed)

Real Taranis → RPi → Jetson → FC. In Mission Planner's RC-calibration screen
verify **all 16 channels** move correctly, then run the failsafe checklist
from the Safety section.

## Measured performance (bench, 2026-07-16)

RPi Zero 2 W (`crsf_spi` @ 921600, 4 Msps SPI) → home WiFi → Jetson Orin
Nano, real Taranis input:

| Metric | Value |
|---|---|
| Gateway output | 185–190 valid pkt/s (~4 % of frames CRC-dropped by the soft-UART decode, never forwarded) |
| UDP loss (30 s tap) | **0** / 5570 packets, 0 out-of-order |
| UDP jitter (relative) | p50 0.7 ms, p95 3.0 ms, max 23 ms |
| Pi↔Jetson UDP RTT (100 Hz, 10 s) | p50 3.5 ms, p95 6.8 ms → one-way ≈ 1.7 ms |
| Bridge → mock FC | `RC_CHANNELS_OVERRIDE` 50.1 Hz, `HEARTBEAT` 1 Hz, 16 ch scaled, chan17/18 = 0, sysid 255 |

The SPI sample rate matters: at 921600 baud, 4 MHz decodes best — 8 MHz
doubles the CRC error rate and the "natural" 10×-baud 9.2 MHz decodes
nothing (hence the 4 MHz cap in `crsf_reader.py`).

## Known caveats

- The `0` / `UINT16_MAX` ("ignore" / "release") semantics for extension
  channels 9–18 of `RC_CHANNELS_OVERRIDE` must be **verified against the
  current `common.xml` and on SITL** — the bridge sends chan17/18 = 0.
- ArduPilot parameter names/ranges differ slightly between Copter, Plane and
  Rover — check the docs for your firmware version.
- The gateway sends one UDP packet per CRSF frame, so the packet rate
  follows the radio (typically 50–150 Hz); the bridge downsamples to
  `override_rate_hz`.
