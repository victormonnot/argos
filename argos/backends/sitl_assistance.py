"""Independent, simulation-only selective assistance for the radio bench.

This executable is deliberately separate from browser flight ownership. It
never arms, selects a mode, sends a heartbeat, or owns pilot throttle/roll/AUX.
The launcher supplies genuine image observations on the shared host monotonic
clock. Only source 1/1, recent ArduCopter SIMSTATE and the checked bench profile
admit corrections; these are accidental-hardware guards, not authentication.

stdin accepts bounded JSON lines: observe, engage, stop, and a maximum two-second
disarmed/landed arbitration probe. stdout emits JSON status and event lines.
An explicit new engage is required after a switch-off, loss, or process restart.
Killing this process leaves override expiry to the flight controller. No other
process, receiver or console is managed here.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
import selectors
import signal
import sys
import time

from argos.backends.mavlink import MavlinkLink, SequenceScope, TcpTransport
from argos.console.framing import FramingControl


TICK_SECONDS = .05
STATUS_SECONDS = .1
PARAM_INTERVAL = .1
PARAM_MAX_AGE = 15.
STREAM_REQUEST_INTERVAL = 2.
HEARTBEAT_MAX_AGE = 2.5
SIMSTATE_MAX_AGE = 3.
RC_MAX_AGE = .65
LANDED_MAX_AGE = 2.5
MAX_LINE_BYTES = 8192
MAX_PROBE_SECONDS = 2.
RELEASE = (0,) * 8 + (65534,) * 10
COPTER_TYPES = frozenset((2, 3, 4, 13, 14, 15, 29, 35))

# This intentionally rejects the browser-only profile. Values are read from the
# controller, never written here. Readback rotates throughout the session.
REQUIRED_PARAMETERS = {
    "ARMING_SKIPCHK": (0.,), "GPS1_TYPE": (0.,), "GPS2_TYPE": (0.,),
    "MAV_GCS_SYSID": (255.,), "RC_OVERRIDE_TIME": (.5,), "RC_OPTIONS": (0.,),
    "FS_GCS_ENABLE": (0.,), "FS_THR_ENABLE": (1., 3.),
    "RC7_OPTION": (46.,), "RC6_OPTION": (153.,), "FLTMODE_CH": (5.,),
    "RCMAP_PITCH": (2.,), "RCMAP_YAW": (4.,),
    "RC2_MIN": (1100.,), "RC2_MAX": (1900.,), "RC2_TRIM": (1500.,),
    "RC2_REVERSED": (0.,), "RC2_DZ": (0.,),
    "RC4_MIN": (1100.,), "RC4_MAX": (1900.,), "RC4_TRIM": (1500.,),
    "RC4_REVERSED": (0.,), "RC4_DZ": (0.,),
    "SIMPLE": (0.,), "SUPER_SIMPLE": (0.,),
    "ATC_ANGLE_MAX": (20.,), "PILOT_Y_RATE": (202.5,), "PILOT_Y_EXPO": (0.,),
    "MNT1_TYPE": (0.,), "SERVO9_FUNCTION": (0.,), "SERVO10_FUNCTION": (0.,),
    "SERVO11_FUNCTION": (0.,),
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _recent(at, now, limit):
    return at is not None and 0 <= now - at <= limit


def _integer(value, minimum=0, maximum=2**32 - 1):
    return type(value) is int and minimum <= value <= maximum


def loopback_peer(value):
    """Accept one explicit numeric IPv4 loopback TCP endpoint only."""
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
        if host != "127.0.0.1" or not port_text.isdecimal() or not 1024 <= port <= 65535:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("use an explicit SITL 127.0.0.1:port (1024..65535)") from None
    return host, port


class SitlAssistance:
    """Single-owner controller; the event loop owns polling and command order."""

    def __init__(self, link, *, emit=None, clock=None):
        self.link = link
        self.emit = emit or (lambda value: None)
        self.clock = clock
        self.framing = FramingControl(enabled=True)
        self._heartbeat_at = self._simstate_at = self._rc_at = self._landed_at = None
        self._armed = self._mode = self._landed = self._switch = None
        self._identity_valid = False
        self._params = {}
        self._parameter_order = deque(REQUIRED_PARAMETERS)
        self._last_param_request = None
        self._last_stream_request = None
        self._observation_context = None
        self._last_observation_sequence = None
        self._last_observation_received_at = self._last_observation_input_at = None
        self._active = False
        self._probe = None
        self._last_override_tick = None
        self._release_pending = True
        self._closed = False
        self._link_error = ""
        self.sent_count = 0
        self.last_override = None
        self.last_send_at = None
        self.last_rejection = None

    def _event(self, now, event, detail):
        self.emit({"kind": "event", "event": event, "at": now, "detail": detail})

    def append(self, event, now):
        if ((event.system, event.component) != (1, 1)
                or not _number(event.received_at) or not 0 <= event.received_at <= now):
            return
        at, values = event.received_at, event.fields
        if event.type_name == "HEARTBEAT" and (self._heartbeat_at is None or at >= self._heartbeat_at):
            if (all(_integer(values.get(key), maximum=255) for key in
                    ("type", "autopilot", "base_mode", "system_status"))
                    and _integer(values.get("custom_mode")) and values.get("mavlink_version") == 3):
                self._heartbeat_at = at
                self._identity_valid = values["autopilot"] == 3 and values["type"] in COPTER_TYPES
                self._armed = bool(values["base_mode"] & 128)
                self._mode = values["custom_mode"]
                if ((self._active and (not self._identity_valid or not self._armed or self._mode != 0))
                        or (self._probe is not None and (not self._identity_valid or self._armed or self._mode != 0))):
                    self._deactivate(now, "Observed arming, mode or autopilot identity no longer permits assistance", clear=True)
        elif event.type_name == "SIMSTATE" and (self._simstate_at is None or at >= self._simstate_at):
            if all(_number(values.get(key)) for key in
                   ("roll", "pitch", "yaw", "xacc", "yacc", "zacc", "xgyro", "ygyro", "zgyro", "lat", "lng")):
                # Simulation identity only; none of these coordinates is retained.
                self._simstate_at = at
        elif event.type_name == "RC_CHANNELS" and (self._rc_at is None or at >= self._rc_at):
            if (_integer(values.get("chancount"), 7, 18)
                    and all(_integer(values.get(f"chan{i}_raw"), 800, 2200) for i in range(1, 8))):
                self._rc_at = at
                self._switch = values["chan7_raw"]
                # A later high packet in the same poll must never hide takeover.
                if self._switch < 1800 and self._active:
                    self._deactivate(now, "Pilot assistance switch is off", clear=True)
        elif event.type_name == "EXTENDED_SYS_STATE" and (self._landed_at is None or at >= self._landed_at):
            if _integer(values.get("landed_state"), 0, 4):
                self._landed_at = at
                self._landed = values["landed_state"]
                if ((self._active and self._landed != 2)
                        or (self._probe is not None and self._landed != 1)):
                    self._deactivate(now, "Observed landed state no longer permits assistance", clear=True)
        elif event.type_name == "PARAM_VALUE":
            name = values.get("param_id")
            if isinstance(name, bytes):
                name = name.decode("ascii", errors="replace")
            if isinstance(name, str):
                name = name.split("\0", 1)[0]
            if isinstance(name, str) and name in REQUIRED_PARAMETERS and _number(values.get("param_value")):
                previous = self._params.get(name)
                if previous is None or at >= previous[1]:
                    self._params[name] = (float(values["param_value"]), at)
                    if ((self._active or self._probe is not None) and not any(
                            math.isclose(float(values["param_value"]), expected, rel_tol=0., abs_tol=1e-5)
                            for expected in REQUIRED_PARAMETERS[name])):
                        self._deactivate(now, "Observed bench parameter mismatch: " + name, clear=True)

    def simulator_reason(self, now):
        if self._link_error:
            return self._link_error
        if not _recent(self._heartbeat_at, now, HEARTBEAT_MAX_AGE) or not self._identity_valid:
            return "A recent ArduCopter HEARTBEAT from source 1/1 is required"
        if not _recent(self._simstate_at, now, SIMSTATE_MAX_AGE):
            return "A recent SIMSTATE from source 1/1 is required"
        return ""

    def parameters(self, now):
        missing, mismatched = [], []
        for name, allowed in REQUIRED_PARAMETERS.items():
            item = self._params.get(name)
            if item is None or not _recent(item[1], now, PARAM_MAX_AGE):
                missing.append(name)
            elif not any(math.isclose(item[0], value, rel_tol=0., abs_tol=1e-5) for value in allowed):
                mismatched.append(name)
        return missing, mismatched

    def readiness_reason(self, now):
        reason = self.simulator_reason(now)
        if reason:
            return reason
        if not _recent(self._rc_at, now, RC_MAX_AGE):
            return "Recent receiver RC_CHANNELS are required"
        if not _recent(self._landed_at, now, LANDED_MAX_AGE):
            return "A recent EXTENDED_SYS_STATE is required"
        missing, mismatched = self.parameters(now)
        if mismatched:
            return "Bench parameter mismatch: " + ", ".join(mismatched)
        if missing:
            return "Waiting for bench parameter readback: " + ", ".join(missing)
        return ""

    def _vehicle_reason(self, now, *, probe=False):
        reason = self.readiness_reason(now)
        if reason:
            return reason
        if self._mode != 0:
            return "Stabilize mode is required"
        if probe:
            if self._armed is not False or self._landed != 1:
                return "Arbitration probes require disarmed and ON_GROUND reports"
        else:
            if self._armed is not True or self._landed != 2:
                return "Framing requires armed and IN_AIR reports"
            if self._switch is None or self._switch < 1800:
                return "Pilot assistance switch is off"
        return ""

    def _deactivate(self, now, reason, *, clear=False):
        was_active = self._active or self._probe is not None
        self._active = False
        self._probe = None
        if clear:
            self.framing.clear(reason)
        self._release_pending = True
        if was_active:
            self._event(now, "assistance_stopped", reason)

    def command(self, values, now):
        """Handle one explicit request. Rejections never grant authority."""
        op = values.get("op") if isinstance(values, dict) else None
        try:
            if self._closed:
                raise RuntimeError("Assistance worker is closed")
            if not isinstance(values, dict) or not isinstance(op, str):
                raise ValueError("A JSON object with an op string is required")
            if op == "observe":
                if set(values) != {"op", "observation"}:
                    raise ValueError("Observe accepts only observation")
                observation = values["observation"]
                self.framing.observe(observation)
                self._observation_context = None
                self._last_observation_input_at = now
                self._last_observation_sequence = self._last_observation_received_at = None
                if isinstance(observation, dict):
                    self._observation_context = (observation.get("run_id"), observation.get("video_id"))
                    sequence, received_at = observation.get("sequence"), observation.get("received_at")
                    self._last_observation_sequence = sequence if _integer(sequence) else None
                    self._last_observation_received_at = received_at if _number(received_at) and received_at >= 0 else None
                # Validation/freshness is owned by FramingControl, not receipt of
                # this command. Invalid metadata becomes unavailable perception.
            elif op == "engage":
                if set(values) != {"op", "track_id"} or not _integer(values["track_id"], 1):
                    raise ValueError("Engage requires one positive integer track_id")
                if self._active or self._probe is not None:
                    raise RuntimeError("Stop the current assistance before engaging again")
                reason = self._vehicle_reason(now)
                if reason:
                    raise RuntimeError(reason)
                self.framing.clear("Explicit new engagement", reset_loss=True)
                self.framing.select(values["track_id"], self._observation_context, now)
                self.framing.engage(now, profile="pilot_throttle")
                self._active = True
                self._release_pending = False
                self._event(now, "framing_engaged", "Explicit pilot-throttle image framing engagement")
            elif op == "stop":
                if set(values) != {"op"}:
                    raise ValueError("Stop accepts no additional fields")
                self._deactivate(now, "Explicit stop", clear=True)
                self._event(now, "stop_requested", "Release selective assistance; pilot receiver remains independent")
            elif op == "probe":
                if set(values) != {"op", "seconds", "pitch", "yaw"}:
                    raise ValueError("Probe requires seconds, pitch and yaw")
                duration, pitch, yaw = values["seconds"], values["pitch"], values["yaw"]
                if (not _number(duration) or not 0 < duration <= MAX_PROBE_SECONDS
                        or not _integer(pitch, 1440, 1560) or not _integer(yaw, 1440, 1560)
                        or (pitch, yaw) == (1500, 1500)):
                    raise ValueError("Probe requires 0 < seconds <= 2 and non-neutral pitch/yaw within 1440..1560")
                if self._active or self._probe is not None:
                    raise RuntimeError("Stop the current assistance before a probe")
                reason = self._vehicle_reason(now, probe=True)
                if reason:
                    raise RuntimeError(reason)
                self._probe = (now + float(duration), pitch, yaw)
                self._release_pending = False
                self._event(now, "ground_probe_started", "Bounded disarmed override probe; deliberately continues with assistance switch off")
            else:
                raise ValueError("Choose observe, engage, stop or probe")
        except (ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
            self.last_rejection = {"kind": "rejected", "op": op if isinstance(op, str) else None,
                                   "at": now, "reason": str(exc)[:1024]}
            self.emit(dict(self.last_rejection))
            return False
        return True

    def _send(self, message, now):
        result = self.link.send(message, now)
        if not result.accepted:
            raise RuntimeError("MAVLink send failed: " + result.status.value + ": " + result.detail)

    def _override(self, now, pitch=None, yaw=None):
        from pymavlink.dialects.v20 import ardupilotmega as mav
        channels = list(RELEASE)
        if pitch is not None:
            channels[1], channels[3] = pitch, yaw
        self._send(mav.MAVLink_rc_channels_override_message(1, 1, *channels), now)
        self.sent_count += 1
        self.last_override = channels
        self.last_send_at = now
        self._last_override_tick = now

    def tick(self, now):
        if self._closed:
            return
        try:
            for event in self.link.poll(now):
                self.append(event, now)
            if self.clock is not None:
                # Decoding receipts (or emitting a transition diagnostic) may
                # stall. Guidance/send eligibility uses current time afterward.
                now = max(now, self.clock())
            if self._probe is not None:
                reason = self._vehicle_reason(now, probe=True)
                if reason or now >= self._probe[0]:
                    self._deactivate(now, reason or "Ground probe duration elapsed", clear=True)
            if self._active:
                reason = self._vehicle_reason(now)
                self.framing.tick(now, reason)
                if reason or self.framing.phase != "active":
                    self._deactivate(now, reason or self.framing.state(now)["reason"], clear=bool(reason))
            # Release remains possible after a profile mismatch. If the simulator
            # identity itself is stale, the verified FC override timeout owns exit.
            if self._release_pending and not self.simulator_reason(now):
                self._override(now)
                self._release_pending = False
                self._event(now, "channels_released", "All override channels explicitly released")
            if ((self._active or self._probe is not None)
                    and (self._last_override_tick is None or now - self._last_override_tick >= TICK_SECONDS - 1e-9)):
                if self._probe is not None:
                    _, pitch, yaw = self._probe
                else:
                    axes = self.framing.axes(now)
                    pitch = round(1500 - 120 * axes["forward"])
                    yaw = round(1500 + 120 * axes["yaw"])
                self._override(now, pitch, yaw)
            if (not self.simulator_reason(now) and (
                    self._last_stream_request is None or (
                        now - self._last_stream_request >= STREAM_REQUEST_INTERVAL
                        and not _recent(self._landed_at, now, LANDED_MAX_AGE)))):
                from pymavlink.dialects.v20 import ardupilotmega as mav
                # EXTRA3 does not include this message in the pinned binary.
                # Request observation at 5 Hz; ACK is not a landed-state receipt.
                self._send(mav.MAVLink_command_long_message(
                    1, 1, 511, 0, 245., 200000., 0., 0., 0., 0., 0.), now)
                self._last_stream_request = now
                self._event(now, "telemetry_interval_requested", "EXTENDED_SYS_STATE requested at 5 Hz")
            if (not self.simulator_reason(now)
                    and (self._last_param_request is None or now - self._last_param_request >= PARAM_INTERVAL - 1e-9)):
                from pymavlink.dialects.v20 import ardupilotmega as mav
                name = self._parameter_order[0]
                self._parameter_order.rotate(-1)
                self._send(mav.MAVLink_param_request_read_message(1, 1, name.encode("ascii"), -1), now)
                self._last_param_request = now
        except Exception as exc:
            self._link_error = f"Assistance transport unavailable: {type(exc).__name__}: {exc}"[:1024]
            self._deactivate(now, self._link_error, clear=True)
            self._event(now, "transport_error", self._link_error)
            self._closed = True
            self.link.close()

    def status(self, now):
        missing, mismatched = self.parameters(now)
        reason = "Assistance worker is closed" if self._closed else self.readiness_reason(now)
        return {"kind": "status", "at": now, "ready": not bool(reason), "reason": reason,
                "framing": self.framing.state(now), "probe_active": self._probe is not None,
                "probe_remaining_s": max(0., self._probe[0] - now) if self._probe else None,
                "sent_count": self.sent_count, "last_override": self.last_override,
                "last_send_at": self.last_send_at, "last_rejection": self.last_rejection,
                # Input diagnostics do not assert observation/lifecycle admission.
                "last_observation_sequence": self._last_observation_sequence,
                "last_observation_received_at": self._last_observation_received_at,
                "last_observation_input_at": self._last_observation_input_at,
                "missing_params": missing, "mismatched_params": mismatched,
                "parameter_values": {name: item[0] for name, item in self._params.items()},
                "vehicle": {"armed": self._armed, "mode": self._mode,
                            "landed_state": self._landed, "switch7_pwm": self._switch},
                "engage_reason": self._vehicle_reason(now)}

    def close(self, now):
        if self._closed:
            return
        self._deactivate(now, "Worker shutdown", clear=True)
        try:
            if not self.simulator_reason(now):
                self._override(now)
                self._event(now, "channels_released", "Orderly worker shutdown")
        except Exception as exc:
            self._event(now, "shutdown_release_failed", str(exc)[:1024])
        finally:
            self._closed = True
            self.link.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mavlink-peer", required=True, type=loopback_peer)
    args = parser.parse_args()

    def emit(value):
        print(json.dumps(value, separators=(",", ":"), allow_nan=False), flush=True)

    link = MavlinkLink(TcpTransport(peer=args.mavlink_peer),
                       sequence_scope=SequenceScope.CHANNEL, system=255, component=191,
                       started_at=time.monotonic())
    worker = SitlAssistance(link, emit=emit, clock=time.monotonic)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
    pending = bytearray()
    stop = False
    next_tick = time.monotonic()
    last_status = None

    def interrupted(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        while not stop:
            now = time.monotonic()
            commands_received = False
            if selector.select(max(0., next_tick - now)):
                data = os.read(sys.stdin.fileno(), MAX_LINE_BYTES + 1)
                if not data:
                    break
                pending.extend(data)
                # Bound input work; a flooding/invalid control source is a stopped
                # source, never a backlog of commands replayed after a stall.
                if len(pending) > 2 * MAX_LINE_BYTES or pending.count(b"\n") > 64:
                    emit({"kind": "rejected", "op": None, "at": time.monotonic(), "reason": "Input command buffer exceeded"})
                    break
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    now = time.monotonic()
                    if len(line) > MAX_LINE_BYTES:
                        stop = True
                        break
                    try:
                        values = json.loads(line)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        emit({"kind": "rejected", "op": None, "at": now, "reason": "Invalid JSON command"})
                        stop = True
                        break
                    worker.command(values, time.monotonic())
                    commands_received = True
                if len(pending) > MAX_LINE_BYTES:
                    stop = True
            if stop:
                break
            # Read/decode/command handling may take time. Use a new send-time
            # clock, consume new images immediately, and never replay timer ticks.
            now = time.monotonic()
            periodic_due = now >= next_tick
            if commands_received or periodic_due:
                worker.tick(now)
                now = time.monotonic()
                # An input arriving before a periodic tick must not postpone it:
                # the law can update now while its next 20 Hz send remains due.
                if periodic_due or now >= next_tick:
                    next_tick = now + TICK_SECONDS
                if last_status is None or now - last_status >= STATUS_SECONDS - 1e-9:
                    emit(worker.status(now))
                    last_status = now
                if worker._closed:
                    break
    finally:
        worker.close(time.monotonic())
        selector.close()
    return 0 if not worker._link_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
