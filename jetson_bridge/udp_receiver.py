"""UDP receiver for link packets: validation plus seq-based loss detection."""

from __future__ import annotations

import socket
from dataclasses import dataclass

from protocol import link_protocol

_NO_DATA = object()


@dataclass
class RxStats:
    received: int = 0
    invalid: int = 0
    lost: int = 0
    out_of_order: int = 0


class UdpReceiver:
    def __init__(self, listen_ip: str = "0.0.0.0", listen_port: int = 14650,
                 timeout_s: float = 0.02):
        self._timeout_s = timeout_s
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((listen_ip, listen_port))
        self._last_seq = None
        self.stats = RxStats()

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    def poll_latest(self):
        """Block up to ``timeout_s`` for a datagram, then drain the queue.

        Returns the newest valid :class:`LinkPacket`, or ``None`` if nothing
        valid arrived. Draining keeps latency low when packets arrive faster
        than the caller consumes them.
        """
        latest = None
        blocking = True
        while True:
            result = self._recv_one(blocking)
            if result is _NO_DATA:
                break
            blocking = False
            if result is not None:
                latest = result
        return latest

    def _recv_one(self, blocking: bool):
        self._sock.settimeout(self._timeout_s if blocking else 0.0)
        try:
            data, _ = self._sock.recvfrom(2048)
        except (socket.timeout, BlockingIOError):
            return _NO_DATA
        try:
            packet = link_protocol.unpack(data)
        except link_protocol.LinkProtocolError:
            self.stats.invalid += 1
            return None
        self._track_seq(packet.seq)
        self.stats.received += 1
        return packet

    def _track_seq(self, seq: int) -> None:
        if self._last_seq is not None:
            delta = (seq - self._last_seq) % link_protocol.SEQ_MODULO
            if delta == 0 or delta > link_protocol.SEQ_MODULO // 2:
                self.stats.out_of_order += 1
                return  # duplicate/old packet: don't move last_seq backwards
            if delta > 1:
                self.stats.lost += delta - 1
        self._last_seq = seq

    def close(self) -> None:
        self._sock.close()
