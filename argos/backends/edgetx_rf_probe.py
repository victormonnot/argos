"""Finite, disarmed Pocket-to-Betaflight yaw bench with two explicit USB ports.

Use the separately configured ARGOS RF radio profile, propellers removed and
aircraft USB power only. This helper only reads MSP; Pocket messages exercise
the fixed bench pattern. It is not a flight controller or flight validation.
For standalone use, keep betaflight_probe.py and edgetx_probe.py alongside it.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time

if __package__:
    from . import betaflight_probe as msp
    from .edgetx_probe import Lines, ProbeError, open_port
else:
    import betaflight_probe as msp
    from edgetx_probe import Lines, ProbeError, open_port


HELLO = b"ARGOS_RF_YAW_BENCH_V1"
SAFETY_TIMEOUT = .15
ACK_TIMEOUT = .2
ACTIVE_GAP = .25
INTERVAL = .1
ALLOWED_ARMING_FLAGS = 1 << 20  # DSHOT_TELEM on the identified 2026.6.0-alpha.
VALUES = (-128, 0, 128)


def validate_distinct_ports(pocket: str, fc: str):
    for device in (pocket, fc):
        if not device or device != device.strip():
            raise ValueError("ports must be nonempty paths without surrounding whitespace")
    same = os.path.realpath(pocket) == os.path.realpath(fc)
    if not same:
        try:
            same = os.path.samefile(pocket, fc)
        except FileNotFoundError:
            pass  # Opening the explicit nonexistent path will report the error.
    if same:
        raise ValueError("Pocket and flight-controller ports must be different devices")


class RFBench:
    """A single session; any uncertainty ends all further Pocket writes."""

    def __init__(self, pocket, fc, *, clock=time.monotonic, sleep=time.sleep):
        self.pocket = pocket
        self.clock = clock
        self.sleep = sleep
        self.fc = msp.Probe(fc, timeout=SAFETY_TIMEOUT, clock=clock, sleep=sleep)
        self.lines = Lines()
        self.session = secrets.token_hex(4)
        self.identified = False
        self.ready = False
        self.failed = False
        self.next_sequence = 1
        self.last_write = None
        self.last_ack = None
        self.idle_sequence = None
        self.manual_baseline = None

    def _fresh(self, sample):
        now = self.clock()
        times = (sample["started"], sample["status_received"], sample["rc_received"])
        if any(stamp > now or now - stamp > SAFETY_TIMEOUT for stamp in times):
            raise ProbeError("flight-controller safety readout is stale; no further SET")

    def _safety(self):
        if not self.identified:
            raise ProbeError("verify the expected flight controller before reading safety state")
        started = self.clock()
        payload, received = self.fc.request(msp.ReadCommand.STATUS_EX)
        status = msp.decode_status(payload, self.fc.box_ids)
        status_received = self.fc.started + received
        payload, received = self.fc.request(msp.ReadCommand.RC)
        rc = msp.decode_rc(payload)
        sample = {"started": started, "status_received": status_received,
                  "rc_received": self.fc.started + received, "status": status, "rc": rc}
        self._fresh(sample)
        if status["armed"] is not False:
            raise ProbeError("flight controller is ARMED or its armed state is unknown")
        if status["arming_disable_flag_count"] != 29:
            raise ProbeError("unexpected arming-blocker layout; this bench expects 29 flags")
        flags = status["arming_disable_flags"]
        if flags & ~ALLOWED_ARMING_FLAGS:
            raise ProbeError(f"unexpected arming blockers 0x{flags:08x}; only DSHOT_TELEM is allowed")
        if any(not 900 <= rc[name] <= 2100 for name in ("roll", "pitch", "yaw", "throttle")):
            raise ProbeError("primary RC channels are outside the 900..2100 bench range")
        if len(rc["aux"]) < 3:
            raise ProbeError("RC readout must include ARM AUX1 and crash-flip AUX3")
        if not all(900 <= value <= 1050 for value in
                   (rc["throttle"], rc["aux"][0], rc["aux"][2])):
            raise ProbeError("throttle, ARM AUX1 and crash-flip AUX3 must all be low (900..1050)")
        axes = (rc["roll"], rc["pitch"])
        if self.manual_baseline is None:
            if not all(1450 <= value <= 1550 for value in axes):
                raise ProbeError("center roll and pitch (1450..1550) before this yaw-only bench")
            self.manual_baseline = axes
        elif any(abs(value - baseline) > 50 for value, baseline in
                 zip(axes, self.manual_baseline)):
            raise ProbeError("roll or pitch moved more than 50 from the initial baseline")
        return sample

    def _wait(self, expected: bytes, timeout: float, *, monitor=False):
        deadline = self.clock() + timeout
        next_check = self.clock()
        while self.clock() < deadline:
            if monitor and self.clock() >= next_check:
                self._safety()
                next_check = self.clock() + INTERVAL
            lines = self.lines.feed(self.pocket.read(256))
            if self.clock() >= deadline:
                break
            found = False
            for line in lines:
                if line == expected and not found:
                    found = True
                elif line == HELLO and expected != HELLO:
                    continue
                elif line.startswith(b"ARGOS_RF_"):
                    raise ProbeError(f"unexpected RF-bench response: {line!r}")
                elif expected != HELLO and line:
                    raise ProbeError(f"unexpected serial response: {line!r}")
            if found:
                return
            self.sleep(min(.005, max(0., deadline - self.clock())))
        hint = ("; check ARGOS RF, ArgRF, USB-VCP=Lua and USB Serial, with no other serial tool"
                if expected == HELLO else "; stop and inspect the radio/USB connection before another run")
        raise ProbeError(f"timeout waiting for {expected.decode('ascii')}{hint}")

    def _quiet(self, duration, *, monitor=False):
        deadline = self.clock() + max(0., duration)
        next_check = self.clock()
        while self.clock() < deadline:
            if monitor and self.clock() >= next_check:
                self._safety()
                next_check = self.clock() + INTERVAL
            for line in self.lines.feed(self.pocket.read(256)):
                if line and line != HELLO:
                    raise ProbeError(f"unexpected response while waiting: {line!r}")
            self.sleep(min(.005, max(0., deadline - self.clock())))

    def _write(self, text, sample):
        self._fresh(sample)  # Recheck immediately before every Pocket write.
        packet = (text + "\n").encode("ascii")
        started = self.clock()
        if self.pocket.write(packet) != len(packet):
            raise ProbeError("partial Pocket serial write")
        if self.clock() - started > SAFETY_TIMEOUT:
            raise ProbeError("Pocket serial write exceeded the 150 ms bench limit")
        return started

    def _require_ready(self):
        if self.failed or not self.ready:
            raise ProbeError("a fresh FC check and RF-bench handshake are required")

    def connect(self):
        if self.failed or self.ready or self.identified:
            raise ProbeError("start a new connection for another bench run")
        try:
            identity = self.fc.identity()
            expected = {"variant": "BTFL", "api": "1.48", "firmware": "2026.6.0-alpha",
                        "firmware_numbers": [2026, 6, 0], "manufacturer": "BEFH",
                        "board_name": "BETAFPVG473_V2"}
            if any(identity[key] != value for key, value in expected.items()):
                raise ProbeError("unexpected FC identity; expected BEFH BETAFPVG473_V2 / "
                                 "Betaflight 2026.6.0-alpha / MSP 1.48")
            self.identified = True
            self.pocket.reset_input_buffer()
            self._wait(HELLO, 5.)
            sample = self._safety()
            self._write(f"ARGOS_RF_BEGIN {self.session}", sample)
            self._wait(f"ARGOS_RF_READY {self.session}".encode(), 2., monitor=True)
            self.ready = True
            return identity
        except BaseException:
            self.failed = True
            raise

    def set_value(self, value):
        self._require_ready()
        if type(value) is not int or value not in VALUES:
            raise ProbeError("the bench permits only -128, 0 or 128")
        if self.next_sequence > 120:
            raise ProbeError("the RF bench is limited to 120 exchanges")
        sequence = self.next_sequence
        try:
            if self.last_write is not None:
                self._quiet(INTERVAL - (self.clock() - self.last_write))
            sample = self._safety()
            now = self.clock()
            if self.last_write is not None and any(
                    now - stamp > ACTIVE_GAP for stamp in (self.last_write, self.last_ack)):
                raise ProbeError("active command gap exceeded 250 ms; no further SET")
            self.last_write = self._write(f"ARGOS_RF_SET {self.session} {sequence} {value}", sample)
            self._wait(f"ARGOS_RF_ACK {self.session} {sequence} {value}".encode(), ACK_TIMEOUT)
            self.last_ack = self.clock()
            self.next_sequence += 1
            self.idle_sequence = None
            return sequence, sample
        except BaseException:
            self.failed = True
            raise

    def wait_idle(self):
        self._require_ready()
        sequence = self.next_sequence - 1
        if not sequence or self.idle_sequence == sequence:
            raise ProbeError("expiry requires a new acknowledged SET")
        try:
            self._wait(f"ARGOS_RF_IDLE {self.session} {sequence}".encode(), 2., monitor=True)
            sample = self._safety()
            self.idle_sequence = sequence
            self.last_write = self.last_ack = None
            return sequence, sample
        except BaseException:
            self.failed = True
            raise

    def hold_idle(self):
        self._require_ready()
        if self.idle_sequence != self.next_sequence - 1:
            raise ProbeError("a matched expiry report is required before the neutral pause")
        try:
            self._quiet(.5, monitor=True)
        except BaseException:
            self.failed = True
            raise


def show_sample(label, sample):
    rc = sample["rc"]
    print(f"{label} | observed DISARMED RC R/P/Y/T "
          f"{rc['roll']}/{rc['pitch']}/{rc['yaw']}/{rc['throttle']} "
          f"AUX1={rc['aux'][0]} AUX3={rc['aux'][2]} "
          f"blockers=0x{sample['status']['arming_disable_flags']:08x}", flush=True)


def run_pattern(bench):
    for value, count, label in [(0, 10, "0%"), (128, 20, "+12.5%"),
                                (-128, 20, "-12.5%"), (0, 10, "0%")]:
        print(f"Phase {label}: compare received yaw with the previous acknowledged command.", flush=True)
        for _ in range(count):
            sequence, sample = bench.set_value(value)
            show_sample(f"ACK {sequence}/60 command {label}; FC sampled BEFORE this SET", sample)
        if count == 20 and value == 128:
            sequence, sample = bench.wait_idle()
            show_sample(f"IDLE 1/2 after SET {sequence}; FC sampled AFTER expiry report", sample)
            bench.hold_idle()
    sequence, sample = bench.wait_idle()
    show_sample(f"IDLE 2/2 after SET {sequence}; FC sampled AFTER expiry report", sample)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pocket-port", required=True, help="explicit Pocket USB serial device")
    parser.add_argument("--fc-port", required=True, help="different, explicit Betaflight USB serial device")
    args = parser.parse_args(argv)
    try:
        validate_distinct_ports(args.pocket_port, args.fc_port)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    pocket = fc = None
    try:
        print("Disarmed RF yaw bench only: remove propellers, disconnect the flight battery, "
              "and use aircraft USB power. Select the reviewed ARGOS RF profile. "
              "Close Configurator and other serial tools.", flush=True)
        fc = msp.open_port(args.fc_port, timeout=SAFETY_TIMEOUT)
        pocket = open_port(args.pocket_port)
        pocket.write_timeout = SAFETY_TIMEOUT
        bench = RFBench(pocket, fc)
        print("Checking FC identity, radio greeting and fresh disarmed/low-channel state...", flush=True)
        bench.connect()
        print("Expected FC and RF-bench handshake verified.", flush=True)
        run_pattern(bench)
        print("Completed the fixed 60-exchange sequence and 2 expiry reports. "
              "Review received yaw and manual fallback; ACKs alone do not prove aircraft "
              "response. This is not flight validation.", flush=True)
        return 0
    except ImportError:
        print('Install serial support: python -m pip install "pyserial==3.5"', file=sys.stderr)
        return 1
    except (OSError, ValueError, ProbeError, msp.ProbeError) as exc:
        print(f"RF yaw bench stopped: {exc}. No cleanup SET sent.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("RF yaw bench interrupted. No cleanup SET sent.", file=sys.stderr)
        return 130
    finally:
        try:
            if pocket is not None:
                pocket.close()
        finally:
            if fc is not None:
                fc.close()


if __name__ == "__main__":
    raise SystemExit(main())
