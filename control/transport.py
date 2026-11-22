"""UDP transport for motor commands and motor status. No policy lives here.

docs/contracts.md "Messages": motor commands are UDP datagrams and UDP has no
connection. This module only moves bytes. Every decision about what to send
is in control/publisher.py, and every check of what was received is in
control/status_gate.py.

Reads use a buffer one byte larger than the section 3 limit of 8192 bytes,
so an oversize datagram reaches the decoder whole enough to be rejected
rather than silently truncated into something that parses.
"""

from __future__ import annotations

import socket

RECV_BUFFER = 8192 + 1


class UdpTransport:
    """One UDP socket bound locally, sending to one receiver address."""

    def __init__(self, local: tuple[str, int], remote: tuple[str, int]):
        self.remote = remote
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(local)

    @property
    def local_address(self) -> tuple[str, int]:
        return self._sock.getsockname()

    def send(self, payload: bytes) -> None:
        """Send one datagram. Socket errors propagate to the caller."""
        self._sock.sendto(payload, self.remote)

    def recv(self, timeout_s: float) -> bytes | None:
        """Return one received datagram, or None when the timeout passes.

        The source address is not inspected. It is not authorization
        (section 3, "Command authentication"); the status gate authenticates
        every payload.
        """
        self._sock.settimeout(timeout_s)
        try:
            payload, _sender = self._sock.recvfrom(RECV_BUFFER)
        except TimeoutError:
            return None
        return payload

    def close(self) -> None:
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
