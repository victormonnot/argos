"""Nonblocking byte transports with explicit peers and write counts.

Opening a transport sends no MAVLink message and writes no vehicle parameter.
UDP is connected to one configured IPv4 peer; it never learns a destination from
an arbitrary incoming packet. Serial has no background writer or retry queue.
"""
from __future__ import annotations

from ipaddress import IPv4Address
import socket
from typing import Protocol


class Transport(Protocol):
    datagram: bool

    def read(self) -> bytes:
        """At most 65535 bytes; empty when nothing is available."""
        ...

    def write(self, data: bytes) -> int:
        """Bytes accepted locally, not bytes acknowledged by the remote system."""
        ...

    def close(self) -> None:
        ...


def _address(value: tuple[str, int], *, ephemeral: bool) -> tuple[str, int]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("address must be an (IPv4 literal, port) tuple")
    host, port = value
    if not isinstance(host, str):
        raise ValueError("address needs an IPv4 literal")
    host = str(IPv4Address(host))
    if (isinstance(port, bool) or not isinstance(port, int)
            or not (0 if ephemeral else 1) <= port <= 65535):
        raise ValueError("port is outside its supported range")
    return host, port


class UdpTransport:
    """A single peer, complete MAVLink frames per UDP datagram, IPv4 only."""

    datagram = True

    def __init__(self, *, local: tuple[str, int], peer: tuple[str, int]):
        local = _address(local, ephemeral=True)
        peer = _address(peer, ephemeral=False)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._socket.bind(local)
            self._socket.connect(peer)
            self._socket.setblocking(False)
        except Exception:
            self._socket.close()
            raise

    @property
    def local_address(self) -> tuple[str, int]:
        return self._socket.getsockname()

    def read(self) -> bytes:
        try:
            # Large enough for the entire IPv4 UDP payload: no silent truncation.
            return self._socket.recv(65535)
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> int:
        return self._socket.send(data)

    def close(self) -> None:
        self._socket.close()


class SerialTransport:
    """One serial stream. Callers own reconnection after a failed/partial write."""

    datagram = False

    def __init__(self, *, device: str, baudrate: int = 115200):
        if not isinstance(device, str) or not device:
            raise ValueError("serial device must be a nonempty path")
        if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate <= 0:
            raise ValueError("baudrate must be a positive integer")
        try:
            import serial
        except ImportError as exc:
            raise ImportError('install the MAVLink extra: pip install -e ".[mavlink]"') from exc
        self._port = serial.Serial(device, baudrate=baudrate, timeout=0, write_timeout=0)

    def read(self) -> bytes:
        return self._port.read(65535)

    def write(self, data: bytes) -> int:
        return self._port.write(data)

    def close(self) -> None:
        self._port.close()


class TcpTransport:
    """Explicit TCP peer; connection is bounded, subsequent I/O is nonblocking.

    EOF is an error, not an empty poll. Like serial, partial frames are retained
    by MavlinkLink. Connecting emits TCP traffic but no MAVLink message.
    """

    datagram = False

    def __init__(self, *, peer: tuple[str, int]):
        peer = _address(peer, ephemeral=False)
        self._socket = socket.create_connection(peer, timeout=2.)
        try:
            self._socket.setblocking(False)
        except Exception:
            self._socket.close()
            raise

    def read(self) -> bytes:
        try:
            data = self._socket.recv(65535)
        except BlockingIOError:
            return b""
        if not data:
            raise ConnectionError("TCP peer closed the telemetry stream")
        return data

    def write(self, data: bytes) -> int:
        return self._socket.send(data)

    def close(self) -> None:
        self._socket.close()
