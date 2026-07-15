# Link protocol: RPi gateway → Jetson bridge (UDP)

One datagram per successfully parsed CRSF `RC_CHANNELS_PACKED` frame, so the
packet rate follows the transmitter (typically 50–150 Hz). The reference
implementation is [`link_protocol.py`](link_protocol.py) — both sides import
it; nothing else defines the wire format.

## Wire format

Little-endian, fixed **48 bytes**:

| Offset | Field      | Type        | Description |
|-------:|------------|-------------|-------------|
| 0      | `magic`    | `uint16`    | Constant `0x4C43` ("CL" little-endian). Datagrams with any other value are dropped and counted as invalid. |
| 2      | `version`  | `uint8`     | Protocol version. Currently `1`. Receivers drop other versions. |
| 3      | `flags`    | `uint8`     | Bit 0 = `data_valid` (channels field carries real stick data). Bits 1–7 reserved, must be 0. |
| 4      | `seq`      | `uint32`    | Monotonic packet counter, wraps at 2³². Used by the receiver to detect loss and reordering. |
| 8      | `t_us`     | `uint64`    | Sender `CLOCK_MONOTONIC` timestamp in microseconds, for latency measurement. **Not comparable across hosts** — absolute one-way latency is only meaningful when sender and receiver share a clock (same host); across hosts use it for jitter/relative statistics or RTT probes. |
| 16     | `channels` | `uint16×16` | **Raw CRSF channel values, 0..2047** (11-bit range). Deliberately *not* microseconds: the RPi stays "dumb", and all scaling parameters live centrally on the Jetson. |

## Semantics

- The RPi increments `seq` by exactly 1 per sent packet.
- Receiver loss detection: `delta = (seq - last_seq) mod 2³²`; `delta > 1`
  means `delta - 1` packets were lost; `delta == 0` or `delta > 2³¹` is
  counted as out-of-order/duplicate and does not move `last_seq` backwards.
- Scaling to RC microseconds happens on the Jetson:
  `us = round((crsf - 992) * 5 / 8 + 1500)`, then clamped to
  `[us_min, us_max]` (default 988..2012). Anchors: 172→988, 992→1500,
  1811→2012.
- A packet with `data_valid = 0` must not be used for RC output but still
  updates `seq` statistics. (The current gateway only ever sends valid
  packets; the flag exists for protocol evolution, e.g. keepalives.)

## Versioning

Any change to the layout bumps `version` and both sides must be redeployed
together. Adding meaning to reserved flag bits does not require a version
bump as long as a zero bit keeps the old behaviour.
