"""Explicit, single-browser manual control of an isolated ArduCopter simulation.

This service never opens a transport or changes parameters. Its caller must also
restrict enablement to the simulation/loopback TCP configuration. A selected-source
SIMSTATE receipt is an accidental-hardware guard, not authentication, and its
ground-truth fields never enter a flight command. Disabled/unclaimed control is
passive. MANUAL_CONTROL expresses pilot input in AltHold or Stabilize, not a
position target: neutral attitude can still drift. Stabilize has explicit manual
throttle. ArduPilot's arming checks remain authoritative.

One event-loop owner appends receipts, handles HTTP mutations and ticks at 20 Hz.
Commands are attempted once, never queued or retried. Local send, COMMAND_ACK and
observed vehicle state are separate evidence. Loss of browser input revokes its
lease permanently, attempts LAND once, then stops GCS heartbeat and manual input;
the checked SITL GCS failsafe is the independent process/link-loss fallback.
"""
from __future__ import annotations

import math
import secrets
from collections import deque
from copy import deepcopy

from .framing import FramingControl


DIGITAL_INPUT_PARAMETERS = {
    "RCMAP_ROLL": 1., "RCMAP_PITCH": 2., "RCMAP_THROTTLE": 3., "RCMAP_YAW": 4.,
    "RC1_REVERSED": 0., "RC2_REVERSED": 0., "RC3_REVERSED": 0., "RC4_REVERSED": 0.,
    "RC1_MIN": 1100., "RC1_MAX": 1900., "RC1_TRIM": 1500.,
    "RC2_MIN": 1100., "RC2_MAX": 1900., "RC2_TRIM": 1500.,
    "RC3_MIN": 1100., "RC3_MAX": 1900., "RC3_TRIM": 1500.,
    "RC4_MIN": 1100., "RC4_MAX": 1900., "RC4_TRIM": 1500.,
}
REQUIRED_PARAMETERS = {
    "GPS1_TYPE": 0.,
    "GPS2_TYPE": 0.,
    "FS_GCS_ENABLE": 5.,
    "FS_GCS_TIMEOUT": 2.,
    "RC_OVERRIDE_TIME": 3.,
    "FS_OPTIONS": 0.,
    "FLTMODE_CH": 0.,
    "MAV_GCS_SYSID": 255.,
    "PILOT_SPD_UP": 1.,
    "PILOT_SPD_DN": .7,
    "LAND_SPD_MS": .5,
    "ATC_ANGLE_MAX": 20.,
    "RC1_DZ": 0., "RC2_DZ": 0., "RC3_DZ": 0., "RC4_DZ": 0., "THR_DZ": 0.,
    # The airborne throttle transfer is defined for this digital Copter input map.
    **DIGITAL_INPUT_PARAMETERS,
    "FRAME_CLASS": 1., "RC_OPTIONS": 0., "PILOT_THR_BHV": 0.,
}
# The camera law adds its yaw gain and fixed camera checks to the shared digital
# input map. Symmetric endpoints and trims keep neutral attitude valid for both
# manual flight-mode bridges and camera framing.
FRAMING_PARAMETERS = {
    "SIMPLE": 0., "SUPER_SIMPLE": 0.,
    "PILOT_Y_RATE": 202.5, "PILOT_Y_EXPO": 0.,
    "MNT1_TYPE": 0.,
    "SERVO9_FUNCTION": 0., "SERVO10_FUNCTION": 0., "SERVO11_FUNCTION": 0.,
}
AXES = ("forward", "right", "up", "yaw")
INPUT_TIMEOUT = .65
HEARTBEAT_MAX_AGE = 2.
SIMSTATE_MAX_AGE = 3.
LANDED_MAX_AGE = 2.
COMMAND_TIMEOUT = 4.
MODE_CHANGE_TIMEOUT = 1.
THRUST_MAX_AGE = .45
HOVER_MAX_AGE = 1.
HOVER_READ_INTERVAL = .5
BRIDGE_THROTTLE_MIN, BRIDGE_THROTTLE_MAX = .25, .75
MANUAL_INTERVAL = .05
HEARTBEAT_INTERVAL = 1.
PARAMETER_INITIAL_BATCH = 17
PARAMETER_READ_INTERVAL = .1
PARAMETER_READ_WINDOW = 15.
STABILIZE, ALT_HOLD, LAND = 0, 2, 9
MANUAL_MODES = frozenset((STABILIZE, ALT_HOLD))
_MISSING = object()
DO_SET_MODE, ARM_DISARM = 176, 400
COPTER_TYPES = frozenset((2, 3, 4, 13, 14, 15, 29, 35))


class ModeGenerationConflict(RuntimeError):
    """An owned request belongs to an older set of input semantics."""

    def __init__(self, control):
        super().__init__("Flight mode changed; synchronize controls before sending input")
        self.control = control


def _number(value):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value))
    except OverflowError:
        return False


def _integer(value, minimum=0, maximum=255):
    return (not isinstance(value, bool) and isinstance(value, int)
            and minimum <= value <= maximum)


def _age(now, at):
    return None if at is None else max(0., now - at)


class FlightControl:
    """Bounded state and sends; all times are the session's monotonic run clock."""

    def __init__(self, *, enabled=False, system=1, component=1, framing_enabled=False):
        self.enabled = enabled
        self.framing = FramingControl(enabled=enabled and framing_enabled)
        self._required_parameters = {
            **REQUIRED_PARAMETERS, **(FRAMING_PARAMETERS if self.framing.enabled else {}),
        }
        self._landed_state = None
        self.system, self.component = system, component
        self._heartbeat_at = self._simstate_at = self._landed_at = None
        self._armed = self._mode = self._landed = None
        self._identity_valid = False
        self._token = None
        self._claimed_at = self._input_at = None
        self._seq = -1
        self._last_manual_seq = -1
        self._last_mode_input_seq = -1
        self._framing_intent = 0
        self._axes = dict.fromkeys(AXES, 0.)
        self._selected_mode = ALT_HOLD
        self._mode_generation = 0
        self._mode_transition = self._mode_transfer = None
        self._attitude_thrust = self._hover_thrust = self._hover_read_at = None
        self._attitude_thrust_at = self._hover_thrust_at = None
        self._prepared_mode = None
        self._throttle = 0.
        self._last_manual = self._last_gcs = None
        self._params = {}
        self._parameter_reads = deque()
        self._parameter_read_at = self._parameter_read_deadline = None
        self._command = None
        self._phase = "idle" if enabled else "disabled"
        self._error = ""
        self._interruption = None
        self._arm_uncertain = False
        self._recovery_at = None
        self._link_available = True
        self._closed = False

    @staticmethod
    def _dialect():
        # Preserve the dependency-free/passive console when control is disabled.
        from pymavlink.dialects.v20 import ardupilotmega
        return ardupilotmega

    def append(self, event, *, now):
        if (not self.enabled or self._closed
                or (event.system, event.component) != (self.system, self.component)
                or not _number(event.received_at) or event.received_at > now):
            return
        at, fields = event.received_at, event.fields
        if event.type_name == "HEARTBEAT":
            if self._heartbeat_at is not None and at < self._heartbeat_at:
                return
            values = [fields.get(key) for key in
                      ("type", "autopilot", "base_mode", "custom_mode", "system_status")]
            if (not all(_integer(v, maximum=2**32 - 1 if i == 3 else 255)
                        for i, v in enumerate(values))
                    or fields.get("mavlink_version") != 3):
                return
            kind, autopilot, base, custom, _status = values
            self._identity_valid = autopilot == 3 and kind in COPTER_TYPES
            self._heartbeat_at = at
            was_armed = self._armed
            self._armed, self._mode = bool(base & 128), custom
            if was_armed is True and self._armed is False:
                self.framing.clear("Drone disarmed; framing stopped")
            if self._armed is False:
                self._throttle = 0.
                self._attitude_thrust = self._hover_thrust = None
                self._attitude_thrust_at = self._hover_thrust_at = at
            if custom != self._selected_mode:
                self._prepared_mode = None
            self._observe_command(at, now)
            if self._token and self._phase != "landing":
                self._phase = "switching" if self._mode_transition else "armed" if self._armed else (
                    "prepared" if self._prepared_mode == custom else "claimed")
        elif event.type_name == "SIMSTATE":
            # Validate receipt shape, but deliberately never retain its coordinates.
            names = ("roll", "pitch", "yaw", "xacc", "yacc", "zacc",
                     "xgyro", "ygyro", "zgyro", "lat", "lng")
            if all(_number(fields.get(name)) for name in names):
                if self._simstate_at is None or at >= self._simstate_at:
                    self._simstate_at = at
        elif event.type_name == "EXTENDED_SYS_STATE":
            landed = fields.get("landed_state")
            if _integer(landed, maximum=4) and (
                    self._landed_at is None or at >= self._landed_at):
                self._landed_at = at
                self._landed_state = landed
                self._landed = True if landed == 1 else False if landed in (2, 3, 4) else None
        elif event.type_name == "ATTITUDE_TARGET":
            if (self._armed is True and self._claimed_at is not None
                    and at >= self._claimed_at
                    and (self._attitude_thrust_at is None or at >= self._attitude_thrust_at)):
                self._attitude_thrust_at = at
                thrust = fields.get("thrust")
                mask = fields.get("type_mask")
                self._attitude_thrust = ((float(thrust), at)
                    if _integer(mask) and mask == 0
                    and _integer(fields.get("time_boot_ms"), maximum=2**32 - 1)
                    and _number(thrust) and 0. <= thrust <= 1. else None)
        elif event.type_name == "PARAM_VALUE" and self._claimed_at is not None:
            key = fields.get("param_id")
            if isinstance(key, bytes):
                key = key.decode("ascii", errors="replace")
            if isinstance(key, str):
                key = key.split("\0", 1)[0]
            value = fields.get("param_value")
            if (key == "MOT_THST_HOVER" and at >= self._claimed_at
                    and (self._hover_thrust_at is None or at >= self._hover_thrust_at)):
                self._hover_thrust_at = at
                self._hover_thrust = ((float(value), at)
                    if fields.get("param_type") == 9 and _number(value)
                    and .125 <= value <= .6875 else None)
            if (isinstance(key, str) and key in self._required_parameters and _number(value)
                    and at >= self._claimed_at):
                previous = self._params.get(key)
                if previous is None or at >= previous[1]:
                    self._params[key] = (float(value), at)
        elif event.type_name == "COMMAND_ACK":
            command = self._command
            if (command is None or at < command["sent_at"]
                    or fields.get("command") != command["command_id"]
                    or fields.get("target_system", 0) not in (0, 255)
                    or fields.get("target_component", 0) not in (0, 190)
                    or not _integer(fields.get("result"), maximum=9)
                    or command["state"] in ("send_failed", "timeout")):
                return
            result = fields["result"]
            command["ack"], command["ack_at"] = result, at
            if result not in (0, 5):
                command["state"] = "denied"
                command["detail"] = f"MAVLink command rejected (result {result})"
                self._command_error(command["detail"])
                if command["action"] == "arm":
                    self._arm_uncertain = False
                elif command["action"] == "prepare":
                    self._prepared_mode = None
            elif not command["observed"]:
                command["state"] = "accepted"
                command["detail"] = "MAVLink acknowledgement received; vehicle state awaits confirmation"
        if (self._recovery_at is not None and self._identity_valid and self._armed is False
                and self._heartbeat_at is not None and self._heartbeat_at >= self._recovery_at
                and now - self._heartbeat_at <= HEARTBEAT_MAX_AGE
                and (self._mode == LAND or (self._landed is True
                     and self._landed_at is not None and self._landed_at >= self._recovery_at
                     and now - self._landed_at <= LANDED_MAX_AGE))):
            # The old transport has been closed and authority was never restored.
            # Fresh disarmed + LAND/landed evidence resolves a previously unknown
            # arm outcome, including the autopilot's own GCS-failsafe disarming.
            self._arm_uncertain = False
            self._recovery_at = None

    def _observe_command(self, at, now):
        command = self._command
        if (command is None or at < command["sent_at"]
                or command["state"] == "send_failed"):
            return
        action = command["action"]
        observed = ((action == "prepare" and self._mode == command["mode"]
                     and self._armed is False)
                    or (action == "switch_mode" and self._mode_transition is not None
                        and command["state"] not in ("denied", "timeout")
                        and command["sent_at"] < at and now < self._mode_transition["deadline"]
                        and self._token is not None and now - self._input_at < INPUT_TIMEOUT
                        and not self._unavailable(now) and self._profile()["ready"]
                        and self._landed_state == 2 and self._landed_at is not None
                        and now - self._landed_at <= LANDED_MAX_AGE
                        and self._identity_valid and self._armed is True
                        and self._mode == command["mode"])
                    or (action == "arm" and self._armed)
                    or (action == "land" and self._mode == LAND)
                    or (action == "disarm" and self._armed is False))
        if observed:
            first_observation = not command["observed"]
            command["observed"] = True
            command["observed_at"] = at
            if command["state"] != "denied":
                command["state"] = "observed"
                command["detail"] = "State confirmed by vehicle HEARTBEAT"
                if action == "prepare" and first_observation:
                    self._prepared_mode = command["mode"]
                elif action == "switch_mode" and first_observation:
                    transition = self._mode_transition
                    self._selected_mode = self._prepared_mode = command["mode"]
                    self._axes = dict.fromkeys(AXES, 0.)
                    self._throttle = transition["target_throttle"]
                    self._mode_generation += 1
                    self._mode_transfer = {
                        "generation": self._mode_generation,
                        "from_mode": transition["from_mode"], "to_mode": command["mode"],
                        "completed_at": at, "throttle": self._throttle,
                    }
                    self._mode_transition = None
                    self._last_manual = None  # publish target semantics at the next tick
            if action == "disarm":
                self._throttle = 0.
            if action in ("arm", "disarm") or (action == "land" and not self._armed):
                # A buffered disarmed AltHold heartbeat after an unconfirmed arm
                # does not establish that arm failed. LAND + disarmed does show
                # the fallback mode, without treating its ACK as physical state.
                self._arm_uncertain = False

    def _unavailable(self, now):
        if not self.enabled:
            return "Flight controls disabled; observation only"
        if self._closed or not self._link_available:
            return "Control link unavailable"
        if (self._heartbeat_at is None or now - self._heartbeat_at > HEARTBEAT_MAX_AGE
                or not self._identity_valid):
            return "Waiting for a recent ArduCopter HEARTBEAT from the selected source"
        if self._simstate_at is None or now - self._simstate_at > SIMSTATE_MAX_AGE:
            return "Simulation unconfirmed: waiting for a recent SIMSTATE"
        return ""

    def _profile(self):
        values = {key: entry[0] for key, entry in self._params.items()}
        missing = [key for key in self._required_parameters if key not in values]
        mismatched = [key for key, expected in self._required_parameters.items()
                      if key in values and not math.isclose(values[key], expected, abs_tol=1e-5)]
        return {"ready": not missing and not mismatched, "values": values,
                "required": dict(self._required_parameters), "missing": missing,
                "mismatched": mismatched}

    def state(self, now):
        """Pure snapshot: reading status never refreshes input or vehicle receipts."""
        reason = self._unavailable(now)
        return {"at": now, "enabled": self.enabled, "available": not reason,
                "reason": reason, "owned": self._token is not None,
                "phase": self._phase, "lease_started_at": self._claimed_at,
                "last_input_age": _age(now, self._input_at),
                "input_timeout": INPUT_TIMEOUT, "axes": dict(self._axes),
                "selected_mode": self._selected_mode, "throttle": self._throttle,
                "mode_generation": self._mode_generation,
                "mode_transition": deepcopy(self._mode_transition),
                "mode_transfer": deepcopy(self._mode_transfer),
                "mode_switch": self._mode_switch_state(now),
                "prepared": self._prepared_mode == self._selected_mode,
                "input_seq": self._seq,
                "framing": self.framing.state(now, self._framing_vehicle_reason(now)),
                "vehicle": {"armed": self._armed, "mode": self._mode,
                            "landed": self._landed if self._landed_at is not None
                            and now - self._landed_at <= LANDED_MAX_AGE else None,
                            "heartbeat_age": _age(now, self._heartbeat_at),
                            "landed_state": self._landed_state if self._landed_at is not None
                            and now - self._landed_at <= LANDED_MAX_AGE else None},
                "profile": self._profile(),
                "command": None if self._command is None else dict(self._command),
                "interruption": self.interruption,
                "last_error": self._error}

    @property
    def interruption(self):
        """Last owned lease revocation, independent of subsequent command errors."""
        return deepcopy(self._interruption)

    def _command_error(self, detail):
        self._error = (f"{self._interruption['reason']}; {detail}"
                       if self._interruption is not None else detail)

    def _send(self, link, message, now):
        if link is None:
            return "closed", "No MAVLink link"
        try:
            result = link.send(message, now)
            return result.status.value, result.detail
        except Exception as exc:
            # Never retain the message for a later retry after a send exception.
            return "error", str(exc)

    def _manual(self, link, now, *, neutral=False, bridge_throttle=None):
        axes = (dict.fromkeys(AXES, 0.) if neutral else self.framing.axes(now)
                if self.framing.phase in ("active", "takeover") else self._axes)
        if self._armed is not True:
            x = y = r = z = 0  # zero throttle before arm, regardless of UI input
        else:
            # Attitude/yaw input capped at 30%; vertical remains pilot-controlled.
            x, y, r = (round(axes[key] * 300) for key in ("forward", "right", "yaw"))
            # LAND handoff clears attitude but keeps the last explicit Stabilize
            # throttle for this one packet; neither 0 nor 50% is a neutral gas.
            z = (round(self._throttle * 1000) if self._selected_mode == STABILIZE
                 else round((axes["up"] + 1.) * 500))
            if bridge_throttle is not None:
                z = round(bridge_throttle * 1000)
        message = self._dialect().MAVLink_manual_control_message(self.system, x, y, z, r, 0)
        self._last_manual = now
        return self._send(link, message, now)

    def _clear_parameter_reads(self):
        self._parameter_reads.clear()
        self._parameter_read_at = self._parameter_read_deadline = None

    def _read_parameter(self, key, link, now):
        message = self._dialect().MAVLink_param_request_read_message(
            self.system, self.component, key.encode("ascii"), -1)
        return self._send(link, message, now)

    def _read_missing_parameter(self, link, now):
        # ArduPilot's response queue is bounded. After the small initial batch,
        # rotate missing reads at most once per interval without catch-up bursts.
        # This retry window is absolute: input renewals never extend it, and no
        # flight command is retained or retried by this queue.
        if self._parameter_read_deadline is None:
            return
        if now >= self._parameter_read_deadline:
            self._clear_parameter_reads()
            return
        if (self._token is None or self._armed is not False or self._arm_uncertain
                or now < self._parameter_read_at):
            return
        while self._parameter_reads:
            key = self._parameter_reads.popleft()
            if key in self._params:
                continue  # a valid receipt, even a mismatch, is not missing
            self._parameter_reads.append(key)
            self._parameter_read_at = now + PARAMETER_READ_INTERVAL
            status, detail = self._read_parameter(key, link, now)
            if status != "accepted":
                self._revoke(link, now, reason=detail or "Parameter read request not sent", phase="error")
            return
        self._clear_parameter_reads()

    def claim(self, link, now):
        if self._token is not None:
            raise RuntimeError("A browser already owns control")
        self._link_available = link is not None
        reason = self._unavailable(now)
        if reason:
            raise RuntimeError(reason)
        if self._armed is not False or self._arm_uncertain:
            raise RuntimeError("Wait for confirmed disarming before taking control")
        self._token = secrets.token_urlsafe(24)
        self._interruption = None
        self._claimed_at = self._input_at = now
        self._seq = -1
        self._last_manual_seq = -1
        self._last_mode_input_seq = -1
        self._framing_intent = 0
        self.framing.clear("New control lease; select a person", reset_loss=True)
        self._axes = dict.fromkeys(AXES, 0.)
        self._selected_mode = ALT_HOLD
        self._mode_generation = 0
        self._mode_transition = self._mode_transfer = None
        self._attitude_thrust = self._hover_thrust = self._hover_read_at = None
        self._attitude_thrust_at = self._hover_thrust_at = now
        self._prepared_mode = None
        self._throttle = 0.
        self._params = {}
        self._command = None
        self._last_manual = self._last_gcs = None
        self._phase = "claimed"
        self._error = ""
        self._clear_parameter_reads()
        self._parameter_reads.extend(self._required_parameters)
        self._parameter_read_at = now + PARAMETER_READ_INTERVAL
        self._parameter_read_deadline = now + PARAMETER_READ_WINDOW
        for _ in range(min(PARAMETER_INITIAL_BATCH, len(self._parameter_reads))):
            key = self._parameter_reads.popleft()
            self._parameter_reads.append(key)
            status, detail = self._read_parameter(key, link, now)
            if status != "accepted":
                self._revoke(link, now, reason=detail or "Parameter read request not sent", phase="error")
                raise RuntimeError(self._error)
        # Ask for HEARTBEAT and EXTENDED_SYS_STATE at 5 Hz. Sim time may run below
        # wall time; this keeps the same strict receipt-age bounds. A stream ACK
        # never stands in for an actual vehicle-state observation/action ACK.
        for message_id in (0, 245, 83):
            interval = self._dialect().MAVLink_command_long_message(
                self.system, self.component, 511, 0, float(message_id), 200000., 0., 0., 0., 0., 0.)
            status, detail = self._send(link, interval, now)
            if status != "accepted":
                self._revoke(link, now, reason=detail or "Vehicle state request not sent", phase="error")
                raise RuntimeError(self._error)
        self.tick(link, now)
        return {"token": self._token, "control": self.state(now)}

    def _owner(self, token, link, now):
        # Expire before considering the request: an arriving delayed packet must
        # not resurrect input that already exceeded the deadman deadline.
        self._maintain(link, now)
        if not isinstance(token, str) or self._token is None or not secrets.compare_digest(token, self._token):
            raise RuntimeError("Control absent, expired or owned by another browser")

    def _check_mode_generation(self, generation, now):
        # Compatibility is limited to the initial generation. Once an airborne
        # handoff has started, an unversioned delayed packet can never be accepted.
        if generation is _MISSING and self._mode_generation == 0:
            return
        if not _integer(generation, maximum=2**53 - 1):
            if generation is not _MISSING:
                raise ValueError("A nonnegative flight mode generation is required")
            raise ModeGenerationConflict(self.state(now))
        if generation != self._mode_generation:
            raise ModeGenerationConflict(self.state(now))

    def input(self, token, seq, axes, *, link, now, throttle=_MISSING,
              mode_generation=_MISSING):
        self._owner(token, link, now)
        self._check_mode_generation(mode_generation, now)
        if not _integer(seq, maximum=2**53 - 1) or seq <= self._seq:
            raise ValueError("Command sequence must be strictly increasing")
        if (not isinstance(axes, dict) or set(axes) != set(AXES)
                or any(not _number(value) or not -1. <= value <= 1.
                       for value in axes.values())):
            raise ValueError("Four finite axes between -1 and 1 are required")
        if throttle is not _MISSING and (not _number(throttle) or not 0. <= throttle <= 1.):
            raise ValueError("Throttle must be a finite number between 0 and 1")
        if self._mode_transition is not None:
            if any(axes.values()) or (throttle is not _MISSING and throttle != self._throttle):
                raise RuntimeError("Keep inputs released while the flight mode is changing")
            self._seq, self._input_at = seq, now
            return self.state(now)
        if self._selected_mode == ALT_HOLD:
            if throttle is not _MISSING and throttle != 0:
                raise ValueError("AltHold uses the vertical axis; explicit throttle must remain zero")
            accepted_throttle = 0.
        else:
            if axes["up"] != 0:
                raise ValueError("Stabilize uses explicit throttle; the vertical axis must remain zero")
            if self._armed is True and throttle is _MISSING:
                raise ValueError("Armed Stabilize requires an explicit throttle value")
            accepted_throttle = 0. if throttle is _MISSING else float(throttle)
            if self._armed is not True and accepted_throttle != 0:
                raise RuntimeError("Zero throttle required until arming is confirmed")
        if any(axes.values()) or accepted_throttle != 0:
            self._last_manual_seq = seq
            self.framing.stop("Manual input; framing stopped")
        if any(axes.values()) or accepted_throttle != self._throttle:
            self._last_mode_input_seq = seq
        self._seq = seq
        self._input_at = now
        self._axes = {key: float(axes[key]) for key in AXES}
        self._throttle = 0. if self._phase == "landing" else accepted_throttle
        return self.state(now)

    def _framing_vehicle_reason(self, now):
        reason = self._unavailable(now)
        if reason:
            return reason
        if self._token is None:
            return "Take control before selecting or engaging framing"
        if self._input_at is None or now - self._input_at >= INPUT_TIMEOUT:
            return "Pilot input lease expired"
        if self._mode_transition is not None:
            return "Wait for the flight mode change to complete"
        if self._selected_mode != ALT_HOLD or self._mode != ALT_HOLD:
            return "Framing requires AltHold"
        if self._armed is not True:
            return "Take off manually before engaging framing"
        if self._prepared_mode != ALT_HOLD or not self._profile()["ready"]:
            return "Prepared AltHold and the digital GPS-free profile are required"
        if self._phase in ("landing", "released", "expired", "error") or self._recovery_at is not None:
            return "Flight control is handing over or recovering"
        if self._pending():
            return "Wait for the autopilot command to complete"
        if (self._landed_state != 2 or self._landed_at is None
                or now - self._landed_at > LANDED_MAX_AGE):
            return "A recent IN_AIR report is required"
        if any(self._axes.values()) or self._throttle != 0:
            return "Release manual inputs before engaging framing"
        return ""

    def framing_request(self, values, *, link, now, selection_check):
        self._owner(values.get("token"), link, now)
        operation = values.get("operation")
        extra = {"select": {"run_id", "video_id", "frame_sequence", "track_id"},
                 "engage": {"revision", "input_seq"}, "stop": set(),
                 "clear": set(), "closer": set(), "farther": set()}
        if not isinstance(operation, str) or operation not in extra:
            raise ValueError("Unknown framing operation")
        expected = {"token", "operation", "intent"} | extra[operation]
        versioned = operation not in ("stop", "clear")
        optional = {"mode_generation"} if versioned else set()
        if not expected <= set(values) or not set(values) <= expected | optional:
            raise ValueError("Invalid framing fields")
        if versioned:
            # A delayed pre-switch Select must not restore a target after the
            # transition cleared it, even after an AltHold round trip. Stale
            # generations neither mutate the target nor advance operator intent.
            # Stop/Clear retain their unversioned manual-priority contract.
            self._check_mode_generation(values.get("mode_generation", _MISSING), now)
        intent = values["intent"]
        if not _integer(intent, minimum=1, maximum=2**53 - 1):
            raise ValueError("A positive framing intent sequence is required")
        if intent <= self._framing_intent:
            raise RuntimeError("Framing request superseded by a newer operator intent")
        # Even a refused newer operation invalidates an older delayed Engage.
        self._framing_intent = intent
        if not self.framing.enabled:
            raise RuntimeError("Framing is not enabled for this simulation")
        if operation == "stop":
            self.framing.stop("Manual control; framing stopped")
        elif operation == "clear":
            if self.framing.phase == "takeover":
                raise RuntimeError("Take over manually before clearing the target")
            self.framing.clear("Select a person in the camera image")
        elif operation == "select":
            if self._phase == "landing":
                raise RuntimeError("Target selection is unavailable during landing")
            if self._mode_transition is not None:
                raise RuntimeError("Wait for the flight mode change before selecting a target")
            if not all(isinstance(values[key], str) and 1 <= len(values[key]) <= 128
                       for key in ("run_id", "video_id")) or not all(
                       _integer(values[key], minimum=1, maximum=2**53 - 1)
                       for key in ("frame_sequence", "track_id")):
                raise ValueError("A valid camera frame and person ID are required")
            selection_check(values, now)
            self.framing.select(values["track_id"], (values["run_id"], values["video_id"]), now)
        elif operation == "engage":
            if (not _integer(values["revision"], maximum=2**53 - 1)
                    or not _integer(values["input_seq"], maximum=2**53 - 1)):
                raise ValueError("Engage requires an observed revision and pilot input sequence")
            if values["revision"] != self.framing.revision:
                raise RuntimeError("Framing selection changed; inspect it before engaging")
            if not self._last_manual_seq <= values["input_seq"] <= self._seq:
                raise RuntimeError("A manual input superseded this Engage request")
            reason = self._framing_vehicle_reason(now)
            if reason:
                raise RuntimeError(reason)
            self.framing.engage(now)
        else:
            reason = self._framing_vehicle_reason(now)
            if reason:
                raise RuntimeError(reason)
            self.framing.adjust(operation, now)
        return self.state(now)

    def _pending(self):
        return self._command is not None and self._command["state"] in ("sent", "accepted")

    @staticmethod
    def _inverse_stabilize_throttle(thrust, hover):
        """Invert ArduCopter's hover-aware manual throttle, not motor percentage.

        Verified in the supported SITL build (8927564c84f4cdb0):
        Mode::get_pilot_desired_throttle uses this cubic with zero RC deadzone.
        ATTITUDE_TARGET.thrust is the input before angle boost, unlike VFR_HUD.
        Search actual integer MANUAL_CONTROL values including 800-PWM-span
        truncation; this prevents an assumed continuous inverse hiding wire steps.
        """
        expo = min(1., max(-.5, (.5 - hover) / .375))
        def error(z):
            pwm_offset = int(800 * z / 1000)
            t = int(1000 * pwm_offset / 800) / 1000
            return abs(t * (1. - expo) + expo * t**3 - thrust)
        return min(range(1001), key=error) / 1000

    def _mode_switch_state(self, now):
        target = STABILIZE if self._selected_mode == ALT_HOLD else ALT_HOLD
        reason, throttle = self._mode_switch_reason(target, now)
        return {"available": not reason, "reason": reason,
                "target_mode": target, "target_throttle": throttle}

    def _mode_switch_reason(self, target, now):
        reason = self._unavailable(now)
        if reason:
            return reason, None
        if self._token is None:
            return "Take control before changing flight mode", None
        if now - self._input_at >= INPUT_TIMEOUT:
            return "Pilot input lease expired", None
        if self._mode_transition is not None or self._pending():
            return "Wait for the autopilot command to complete", None
        if self._phase == "landing" or self._recovery_at is not None:
            return "Flight control is handing over or recovering", None
        if self._armed is not True or self._arm_uncertain:
            return "Take off manually before changing flight mode", None
        if self._landed_state != 2 or self._landed_at is None or now - self._landed_at > LANDED_MAX_AGE:
            return "A recent IN_AIR report is required", None
        if (self._mode != self._selected_mode or self._prepared_mode != self._selected_mode
                or not self._profile()["ready"]):
            return "Prepared manual mode and the digital GPS-free profile are required", None
        if any(self._axes.values()):
            return "Release direction and climb inputs before changing flight mode", None
        if target == STABILIZE:
            if self._attitude_thrust is None or now - self._attitude_thrust[1] > THRUST_MAX_AGE:
                return "Waiting for recent autopilot throttle demand", None
            if self._hover_thrust is None or now - self._hover_thrust[1] > HOVER_MAX_AGE:
                return "Waiting for the current learned hover thrust", None
            throttle = self._inverse_stabilize_throttle(self._attitude_thrust[0], self._hover_thrust[0])
        else:
            throttle = self._throttle
        if not BRIDGE_THROTTLE_MIN <= throttle <= BRIDGE_THROTTLE_MAX:
            return "Throttle transfer outside the bounded range; stabilize the flight first", None
        return "", throttle if target == STABILIZE else 0.

    def _switch_mode(self, mode, generation, input_seq, *, link, now):
        if mode is _MISSING:
            raise ValueError("Choose Stabilize (0) or AltHold (2) for the mode change")
        self._check_mode_generation(generation, now)
        if generation is _MISSING or not _integer(input_seq, maximum=2**53 - 1):
            raise ValueError("Mode change requires an observed mode generation and input sequence")
        if not self._last_mode_input_seq <= input_seq <= self._seq:
            raise RuntimeError("A manual input superseded this flight mode request")
        if mode == self._selected_mode:
            raise RuntimeError("The selected flight mode is already active")
        reason, target_throttle = self._mode_switch_reason(mode, now)
        if reason:
            raise RuntimeError(reason)
        bridge = target_throttle if mode == STABILIZE else self._throttle
        self.framing.clear("Flight mode changed; select and engage framing again")
        self._axes = dict.fromkeys(AXES, 0.)
        self._mode_generation += 1
        self._mode_transition = {"from_mode": self._selected_mode, "to_mode": mode,
                                 "started_at": now, "deadline": now + MODE_CHANGE_TIMEOUT,
                                 "target_throttle": target_throttle, "bridge_throttle": bridge}
        self._phase = "switching"
        # This bridge is explicit in both modes: Stabilize gas within .25-.75,
        # or bounded AltHold vertical input while DO_SET_MODE is being processed.
        # Keep that one RC override until target HEARTBEAT; never repeatedly send
        # old-mode input while the mode is uncertain. Browser lease renewal stays
        # live, but GCS heartbeat also pauses: the bridge MANUAL_CONTROL refreshes
        # ArduPilot's GCS clock, preserving its 2s-vs-3s fallback/override margin.
        status, detail = self._manual(link, now, neutral=True, bridge_throttle=bridge)
        if status != "accepted":
            self._revoke(link, now, reason=detail or "Mode-change throttle bridge not sent; landing requested",
                         phase="error")
            return self.state(now)
        if not self._issue("switch_mode", link, now, mode=mode):
            self._revoke(link, now, reason="Flight mode command not sent reliably; landing requested", phase="error")
        else:
            self._error = ""
        return self.state(now)

    def _issue(self, action, link, now, *, mode=None):
        command_id = DO_SET_MODE if action in ("prepare", "switch_mode", "land") else ARM_DISARM
        params = ([1., float(mode if action == "switch_mode" else
                            self._selected_mode if action == "prepare" else LAND)]
                  if command_id == DO_SET_MODE else [1. if action == "arm" else 0., 0.])
        message = self._dialect().MAVLink_command_long_message(
            self.system, self.component, command_id, 0, *params, 0., 0., 0., 0., 0.)
        status, detail = self._send(link, message, now)
        self._command = {"action": action, "command_id": command_id,
                         "sent_at": now, "transport": status,
                         "ack": None, "ack_at": None, "observed": False,
                         "observed_at": None,
                         "state": "sent" if status == "accepted" else "send_failed",
                         "detail": "Sent locally; awaiting confirmation"
                         if status == "accepted" else detail or "Command not sent"}
        if action in ("prepare", "switch_mode"):
            self._command["mode"] = mode if action == "switch_mode" else self._selected_mode
        if action == "arm" and status in ("accepted", "partial", "error"):
            self._arm_uncertain = True
        if status != "accepted":
            self._command_error(self._command["detail"])
        return status == "accepted"

    def action(self, token, action, *, link, now, mode=_MISSING,
               mode_generation=_MISSING, input_seq=_MISSING):
        self._owner(token, link, now)
        if action not in ("prepare", "switch_mode", "arm", "land", "disarm", "release"):
            raise ValueError("Unknown flight-control action")
        if mode is not _MISSING and (action not in ("prepare", "switch_mode") or not _integer(mode)
                                      or mode not in MANUAL_MODES):
            raise ValueError("Preparation mode must be Stabilize (0) or AltHold (2)")
        if action != "switch_mode" and (mode_generation is not _MISSING or input_seq is not _MISSING):
            raise ValueError("Mode generation and input sequence belong to an airborne mode change")
        if action == "release":
            self._revoke(link, now, reason="Control released", phase="released")
            return self.state(now)
        reason = self._unavailable(now)
        if reason:
            raise RuntimeError(reason)
        if action == "land":
            self.framing.clear("Landing requested")
            if self._phase == "landing":
                raise RuntimeError("Landing has already been requested")
            self._axes = dict.fromkeys(AXES, 0.)
            if self._mode_transition is None:
                self._manual(link, now, neutral=True)
            self._mode_transition = None
            self._throttle = 0.
            self._prepared_mode = None
            self._phase = "landing"
            if self._issue("land", link, now):
                self._error = ""
            return self.state(now)
        if action == "switch_mode":
            return self._switch_mode(mode, mode_generation, input_seq, link=link, now=now)
        if self._pending():
            raise RuntimeError("Wait for confirmation of the previous command")
        if action in ("prepare", "arm"):
            if any(self._axes.values()) or self._throttle != 0:
                raise RuntimeError("Release all inputs before preparing or arming")
            if self._armed is not False or self._arm_uncertain:
                raise RuntimeError("Confirmed disarming required")
            if action == "prepare":
                if (self._landed is not True or self._landed_at is None
                        or now - self._landed_at > LANDED_MAX_AGE):
                    raise RuntimeError("Preparation rejected: recent landed state required")
                self._selected_mode = ALT_HOLD if mode is _MISSING else mode
                self._prepared_mode = None
                self._throttle = 0.
            if action == "arm":
                if not self._profile()["ready"]:
                    raise RuntimeError("GPS-free SITL / fallback profile unconfirmed; arming rejected")
                if (self._mode != self._selected_mode
                        or self._prepared_mode != self._selected_mode):
                    raise RuntimeError("Prepare and confirm the selected mode (AltHold or Stabilize) before arming")
            status, detail = self._manual(link, now, neutral=True)
            if status != "accepted":
                self._error = detail or "Neutral input not sent"
                raise RuntimeError(self._error)
            self.framing.stop("Manual takeoff; framing must be engaged in the air")
            self._phase = "prepared" if self._prepared_mode == self._selected_mode else "claimed"
        elif action == "disarm":
            if self._armed is False:
                raise RuntimeError("The vehicle is already disarmed")
            if (self._landed is not True or self._landed_at is None
                    or now - self._landed_at > LANDED_MAX_AGE):
                raise RuntimeError("Disarming rejected: recent landed state required")
        if self._issue(action, link, now):
            self._error = ""
        return self.state(now)

    def _revoke(self, link, now, *, reason, phase):
        self._clear_parameter_reads()
        if self._token is None:
            return
        self._interruption = {"at": now, "lease_started_at": self._claimed_at,
                              "reason": str(reason),
                              "framing_loss": self.framing.last_loss
                              if self.framing.phase == "takeover" else None}
        self.framing.clear(reason)
        handoff_started = self._phase == "landing"
        switching = self._mode_transition is not None
        self._mode_transition = None
        self._token = None
        self._axes = dict.fromkeys(AXES, 0.)
        self._phase = phase
        self._error = reason
        # Do not extend old motion while handing over to LAND. If the transport
        # is gone no send is possible; stopping heartbeat triggers SITL's profile.
        if (not handoff_started and link is not None and (self._armed is True or self._arm_uncertain)
                and not (self._command and self._command["action"] == "land")):
            if not switching:
                self._manual(link, now, neutral=True)
            self._issue("land", link, now)
        self._throttle = 0.
        self._prepared_mode = None

    def _maintain(self, link, now):
        self._link_available = link is not None
        command = self._command
        if command and command["state"] in ("sent", "accepted") and now - command["sent_at"] > COMMAND_TIMEOUT:
            command["state"] = "timeout"
            command["detail"] = "Vehicle state unconfirmed before timeout; no automatic retry"
            self._command_error(command["detail"])
        if self._token is not None:
            reason = self._unavailable(now)
            if reason:
                self._revoke(link, now, reason=reason, phase="error")
            elif now - self._input_at >= INPUT_TIMEOUT:
                self._revoke(link, now, reason="Browser inputs expired; control released", phase="expired")
            elif self._mode_transition is not None and (
                    now >= self._mode_transition["deadline"]
                    or self._command["state"] in ("denied", "send_failed", "timeout")
                    or self._armed is not True):
                self._revoke(link, now, reason="Flight mode change was not confirmed; landing requested",
                             phase="error")
            elif self._armed is True and self._mode != self._selected_mode and self._phase != "landing":
                self._revoke(link, now, reason="Mode changed outside web flight controls", phase="released")
            elif self._armed is True and not self._profile()["ready"]:
                self._revoke(link, now, reason="Simulation profile changed during flight control", phase="error")

        if self._token is not None and self._phase != "landing":
            self.framing.tick(now, self._framing_vehicle_reason(now))
            if self.framing.takeover_due(now):
                failure = self.framing.state(now)["reason"]
                self._revoke(link, now, reason=f"{failure}; no manual takeover within 2 seconds. Landing requested", phase="released")

    def tick(self, link, now):
        if not self.enabled or self._closed:
            return
        self._maintain(link, now)
        if self._token is None or self._phase == "landing":
            # LAND may be denied, dropped or unconfirmed. Do not let an active
            # browser keep GCS failsafe inhibited after manual input has stopped:
            # the checked 2s GCS LAND fallback precedes the 3s RC override expiry.
            # A new explicit ground preparation leaves this phase and resumes
            # heartbeat/manual input; a landing receipt alone never does.
            return
        if self._mode_transition is not None:
            # Both clocks start with the bridge MANUAL_CONTROL. A later GCS
            # heartbeat could move the 2s failsafe past the 3s bridge override.
            # The absolute 1s mode deadline expires before either autopilot bound.
            return
        if self._last_gcs is None or now - self._last_gcs >= HEARTBEAT_INTERVAL:
            message = self._dialect().MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3)
            status, detail = self._send(link, message, now)
            self._last_gcs = now
            if status != "accepted":
                self._revoke(link, now, reason=detail or "GCS HEARTBEAT not sent", phase="error")
                return
        if self._last_manual is None or now - self._last_manual >= MANUAL_INTERVAL:
            status, detail = self._manual(link, now)
            if status != "accepted":
                self._revoke(link, now, reason=detail or "Manual input not sent", phase="error")
        self._read_missing_parameter(link, now)
        if (self._token is not None and self._mode_transition is None and self._armed is True
                and self._selected_mode == ALT_HOLD
                and (self._hover_read_at is None or now - self._hover_read_at >= HOVER_READ_INTERVAL)):
            self._hover_read_at = now
            status, detail = self._read_parameter("MOT_THST_HOVER", link, now)
            if status != "accepted":
                self._revoke(link, now, reason=detail or "Hover-thrust parameter request not sent", phase="error")

    def check_reconfigure(self):
        if self.enabled and (self._token is not None or self._armed is True or self._arm_uncertain):
            raise RuntimeError("Release control and wait for disarming before changing sources")

    def check_reconnect(self):
        if self._token is not None:
            raise RuntimeError("Release control before reopening MAVLink reception")

    def begin_reconnect(self, now):
        """Resume only same-source reception; retain evidence of unresolved flight."""
        self.check_reconnect()
        self._recovery_at = now
        self._heartbeat_at = self._simstate_at = self._landed_at = None
        self._identity_valid = False
        self._landed = self._landed_state = None
        self.framing.clear("MAVLink source reopening")
        self._link_available = False
        self._claimed_at = None
        self._params = {}
        self._clear_parameter_reads()

    def close(self, link, now):
        self._revoke(link, now, reason="Flight-control session stopped", phase="released")
        self._closed = True
