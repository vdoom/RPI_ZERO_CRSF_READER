"""End-to-end latency and loss measurement for the RC link.

Modes:

  tap          Listen for gateway link packets and report seq loss plus
               latency statistics from the packet's t_us timestamp.
               IMPORTANT: t_us is the sender's monotonic clock. Absolute
               one-way latency is only meaningful when sender and receiver
               run on the SAME host. Across hosts, use the "relative to best
               packet" numbers (jitter) or the rtt mode.
               Run it INSTEAD of the bridge (same listen port), or point the
               gateway at a separate port.

  echo-server  Run on the far device; echoes probe datagrams straight back.

  rtt          Send timestamped probes to an echo-server and report
               round-trip time - a clock-independent measure of the network
               leg (one-way ~ RTT/2).

Examples:
  # on the Jetson (bridge stopped):
  python tools/latency_probe.py tap --port 14650 --duration 10

  # network RTT RPi <-> Jetson:
  python tools/latency_probe.py echo-server --port 14651        # on Jetson
  python tools/latency_probe.py rtt --target 192.168.1.20:14651 # on RPi
"""

import argparse
import pathlib
import socket
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from protocol import link_protocol  # noqa: E402

RTT_MAGIC = b"LPRB"
RTT_STRUCT = struct.Struct("<4sIQ")  # magic, seq, t_us


def percentile(sorted_values, pct):
    if not sorted_values:
        return float("nan")
    index = min(len(sorted_values) - 1,
                int(round(pct / 100.0 * (len(sorted_values) - 1))))
    return sorted_values[index]


def report_us(label, values_us):
    values = sorted(values_us)
    print(f"  {label}: n={len(values)}  "
          f"p50={percentile(values, 50) / 1000:.2f} ms  "
          f"p95={percentile(values, 95) / 1000:.2f} ms  "
          f"max={values[-1] / 1000:.2f} ms" if values else
          f"  {label}: no samples")


def run_tap(args) -> int:
    from jetson_bridge.udp_receiver import UdpReceiver
    receiver = UdpReceiver(args.bind, args.port, timeout_s=0.1)
    print(f"listening on udp://{args.bind}:{receiver.port} "
          f"for {args.duration:.0f} s ...")
    deltas = []
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        packet = receiver.poll_latest()
        if packet is not None:
            deltas.append(link_protocol.monotonic_us() - packet.t_us)
    stats = receiver.stats
    total_seen = stats.received + stats.lost
    print(f"\npackets: received={stats.received} lost={stats.lost} "
          f"out-of-order={stats.out_of_order} invalid={stats.invalid}")
    if total_seen:
        print(f"loss: {stats.lost / total_seen:.2%}")
    if not deltas:
        print("no packets captured - is the gateway pointed at this host/port?")
        receiver.close()
        return 1
    minimum = min(deltas)
    print("\nlatency (ONLY valid if sender runs on this same host):")
    report_us("absolute", deltas)
    print("latency relative to best-observed packet (cross-host jitter):")
    report_us("relative", [d - minimum for d in deltas])
    receiver.close()
    return 0


def run_echo_server(args) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))
    print(f"echo server on udp://{args.bind}:{args.port}, Ctrl+C to stop")
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            sock.sendto(data, addr)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        sock.close()
    return 0


def run_rtt(args) -> int:
    host, _, port = args.target.partition(":")
    target = (host, int(port or 14651))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.0)
    period = 1.0 / args.rate
    deadline = time.monotonic() + args.duration
    next_send = time.monotonic()
    seq = 0
    rtts = []
    pending = {}
    print(f"probing {target[0]}:{target[1]} at {args.rate:.0f} Hz "
          f"for {args.duration:.0f} s ...")
    while time.monotonic() < deadline or pending:
        now_t = time.monotonic()
        if now_t >= next_send and now_t < deadline:
            pending[seq] = link_protocol.monotonic_us()
            sock.sendto(RTT_STRUCT.pack(RTT_MAGIC, seq,
                                        pending[seq]), target)
            seq += 1
            next_send += period
        try:
            data, _ = sock.recvfrom(2048)
        except (BlockingIOError, socket.timeout):
            if now_t >= deadline:
                time.sleep(0.05)
                break  # grace drain done
            time.sleep(0.001)
            continue
        if len(data) != RTT_STRUCT.size:
            continue
        magic, rx_seq, _ = RTT_STRUCT.unpack(data)
        if magic != RTT_MAGIC or rx_seq not in pending:
            continue
        rtts.append(link_protocol.monotonic_us() - pending.pop(rx_seq))
    sock.close()
    lost = seq - len(rtts)
    print(f"\nprobes: sent={seq} answered={len(rtts)} lost={lost} "
          f"({(lost / seq if seq else 0):.2%})")
    if not rtts:
        print("no echoes - is echo-server running on the target?")
        return 1
    print("round-trip time (clock-independent):")
    report_us("rtt", rtts)
    print("one-way network estimate ~ RTT/2:")
    report_us("rtt/2", [r / 2 for r in rtts])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    tap = sub.add_parser("tap", help="listen for gateway link packets")
    tap.add_argument("--bind", default="0.0.0.0")
    tap.add_argument("--port", type=int, default=14650)
    tap.add_argument("--duration", type=float, default=10.0)

    echo = sub.add_parser("echo-server", help="echo probe datagrams back")
    echo.add_argument("--bind", default="0.0.0.0")
    echo.add_argument("--port", type=int, default=14651)

    rtt = sub.add_parser("rtt", help="round-trip probe against echo-server")
    rtt.add_argument("--target", required=True, help="host:port of echo-server")
    rtt.add_argument("--rate", type=float, default=100.0)
    rtt.add_argument("--duration", type=float, default=10.0)

    args = ap.parse_args()
    if args.mode == "tap":
        return run_tap(args)
    if args.mode == "echo-server":
        return run_echo_server(args)
    return run_rtt(args)


if __name__ == "__main__":
    sys.exit(main())
