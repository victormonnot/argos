"""USB display handshake, finite exchanges and error cleanup without a radio."""
from collections import deque
import errno
import os
import select
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from argos.backends import edgetx_probe as probe


class Clock:
    now = 0.

    def clock(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Port:
    def __init__(self, chunks=(), *, ack=True, partial=False):
        self.chunks = deque(chunks)
        self.writes = []
        self.ack = ack
        self.partial = partial
        self.closed = False

    def reset_input_buffer(self):
        # Chunks model new bytes received after opening, not an old backlog.
        pass

    def read(self, maximum):
        assert maximum == 256
        return self.chunks.popleft() if self.chunks else b""

    def write(self, data):
        self.writes.append(data)
        if self.ack:
            number = data.removeprefix(b"ARGOS_USB_PING ").strip()
            self.chunks.extend([b"ARGOS_USB_ACK ", number + b"\n"])
        return len(data) - int(self.partial)

    def close(self):
        self.closed = True


def make(port):
    clock = Clock()
    return probe.DisplayProbe(port, clock=clock.clock, sleep=clock.sleep), clock


@pytest.mark.parametrize("noise", [b"", b"CLI> ", b"$M>\x00\x01\x01", b"xARGOS_USB_DISPLAY_V1\n",
                                  b"ARGOS_USB_DISPLAY_V1 without newline"])
def test_no_host_write_without_exact_tool_greeting(noise):
    port = Port([noise])
    display, clock = make(port)
    with pytest.raises(probe.ProbeError, match="timeout"):
        display.connect()
    assert clock.now == pytest.approx(5.)
    with pytest.raises(probe.ProbeError):
        display.ping()
    assert port.writes == []


def test_fragmented_greeting_and_acknowledgements_only_allow_fixed_pings():
    port = Port([b"boot noise\nARGOS_USB_", b"DISPLAY_V1\r\n"])
    display, _ = make(port)
    display.connect()
    assert port.writes == []
    for count in range(1, 121):
        assert display.ping() == count
    with pytest.raises(probe.ProbeError, match="120"):
        display.ping()
    assert port.writes == [f"ARGOS_USB_PING {count}\n".encode() for count in range(1, 121)]


def test_oversized_line_cannot_smuggle_a_greeting_suffix():
    port = Port([b"x" * 64 + probe.READY + b"\n"])
    display, _ = make(port)
    with pytest.raises(probe.ProbeError, match="oversized"):
        display.connect()
    assert port.writes == []


@pytest.mark.parametrize("response,partial", [(b"", False), (b"ARGOS_USB_ACK 99\n", False),
                                               (b"", True)])
def test_exchange_failure_is_final_without_retry(response, partial):
    port = Port([probe.READY + b"\n"], ack=False, partial=partial)
    display, _ = make(port)
    display.connect()
    port.chunks.append(response)
    with pytest.raises(probe.ProbeError):
        display.ping()
    with pytest.raises(probe.ProbeError):
        display.ping()
    assert port.writes == [b"ARGOS_USB_PING 1\n"]


def test_connect_does_not_reuse_backlog():
    port = Port([probe.READY + b"\n"])
    port.reset_input_buffer = port.chunks.clear
    display, _ = make(port)
    with pytest.raises(probe.ProbeError, match="timeout"):
        display.connect()
    assert port.writes == []


@pytest.mark.parametrize("bad_count", ["0", "121", "-1"])
def test_cli_rejects_unbounded_run_before_open(monkeypatch, bad_count):
    monkeypatch.setattr(probe, "open_port", lambda *_: pytest.fail("opened a device"))
    with pytest.raises(SystemExit) as exc:
        probe.main(["--port", "unused", "--samples", bad_count])
    assert exc.value.code == 2


def test_cli_reports_acknowledged_count_and_closes(monkeypatch, capsys):
    port = Port([probe.READY + b"\n"])
    display, _ = make(port)
    monkeypatch.setattr(probe, "open_port", lambda _: port)
    monkeypatch.setattr(probe, "DisplayProbe", lambda _: display)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    assert probe.main(["--port", "unused", "--samples", "3"]) == 0
    assert port.closed
    assert "Completed 3 display exchanges" in capsys.readouterr().out


@pytest.mark.parametrize("error,code", [(probe.ProbeError("missing script"), 1),
                                       (OSError("unplugged"), 1), (KeyboardInterrupt(), 130)])
def test_cli_error_and_interrupt_close_port(monkeypatch, error, code):
    port = Port()
    monkeypatch.setattr(probe, "open_port", lambda _: port)
    def fail():
        raise error
    monkeypatch.setattr(probe, "DisplayProbe", lambda _: SimpleNamespace(connect=fail))
    assert probe.main(["--port", "unused"]) == code
    assert port.closed


def test_list_ports_does_not_open(monkeypatch, capsys):
    list_ports = pytest.importorskip("serial.tools.list_ports")
    monkeypatch.setattr(probe, "open_port", lambda *_: pytest.fail("opened a device"))
    monkeypatch.setattr(list_ports, "comports", lambda: [SimpleNamespace(
        device="/dev/example", description="Synthetic USB", vid=1, pid=2)])
    assert probe.main(["--list-ports"]) == 0
    assert '"port": "/dev/example"' in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX pseudo-terminal")
def test_standalone_cli_with_real_pyserial_and_a_synthetic_radio():
    pytest.importorskip("serial")
    import tty

    master, slave = os.openpty()
    tty.setraw(slave)
    child = None
    received = bytearray()
    requests = []
    try:
        child = subprocess.Popen([sys.executable, probe.__file__, "--port", os.ttyname(slave),
                                  "--samples", "2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        deadline = time.monotonic() + 8.
        next_greeting = 0.
        while child.poll() is None and time.monotonic() < deadline:
            if time.monotonic() >= next_greeting:
                os.write(master, probe.READY + b"\n")
                next_greeting = time.monotonic() + .1
            if select.select([master], [], [], .02)[0]:
                try:
                    received.extend(os.read(master, 256))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                while b"\n" in received:
                    line, _, remainder = received.partition(b"\n")
                    received[:] = remainder
                    requests.append(bytes(line))
                    expected = f"ARGOS_USB_PING {len(requests)}".encode()
                    assert line == expected
                    os.write(master, f"ARGOS_USB_ACK {len(requests)}\n".encode())
        stdout, stderr = child.communicate(timeout=1.)
        assert child.returncode == 0, (stdout, stderr)
        assert requests == [b"ARGOS_USB_PING 1", b"ARGOS_USB_PING 2"]
        assert not received
        assert "Completed 2 display exchanges" in stdout
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
        os.close(master)
        os.close(slave)
