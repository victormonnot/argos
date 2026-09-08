"""Explicit, single-browser manual control of an isolated ArduCopter simulation.

This service never opens a transport or changes parameters. Its caller must also
restrict enablement to the simulation/loopback TCP configuration. A selected-source
SIMSTATE receipt is an accidental-hardware guard, not authentication, and its
ground-truth fields never enter a flight command. Disabled/unclaimed control is
passive. MANUAL_CONTROL expresses pilot input in AltHold, not a position target:
neutral attitude can still drift. ArduPilot's arming checks remain authoritative.

One event-loop owner appends receipts, handles HTTP mutations and ticks at 20 Hz.
Commands are attempted once, never queued or retried. Local send, COMMAND_ACK and
observed vehicle state are separate evidence. Loss of browser input revokes its
lease permanently, attempts LAND once, then stops GCS heartbeat and manual input;
the checked SITL GCS failsafe is the independent process/link-loss fallback.
"""
from __future__ import annotations

import math
import secrets


REQUIRED_PARAMETERS = {
    "GPS1_TYPE": 0.,
    "GPS2_TYPE": 0.,
    "FS_GCS_ENABLE": 5.,
    "FS_GCS_TIMEOUT": 2.,
    "RC_OVERRIDE_TIME": .5,
    "FS_OPTIONS": 0.,
    "FLTMODE_CH": 0.,
    "MAV_GCS_SYSID": 255.,
    "PILOT_SPD_UP": 1.,
    "PILOT_SPD_DN": .7,
    "LAND_SPD_MS": .5,
    "ATC_ANGLE_MAX": 20.,
}
AXES = ("forward", "right", "up", "yaw")
INPUT_TIMEOUT = .65
HEARTBEAT_MAX_AGE = 2.
SIMSTATE_MAX_AGE = 3.
LANDED_MAX_AGE = 2.
COMMAND_TIMEOUT = 4.
MANUAL_INTERVAL = .05
HEARTBEAT_INTERVAL = 1.
ALT_HOLD, LAND = 2, 9
DO_SET_MODE, ARM_DISARM = 176, 400
COPTER_TYPES = frozenset((2, 3, 4, 13, 14, 15, 29, 35))


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

    def __init__(self, *, enabled=False, system=1, component=1):
        self.enabled = enabled
        self.system, self.component = system, component
        self._heartbeat_at = self._simstate_at = self._landed_at = None
        self._armed = self._mode = self._landed = None
        self._identity_valid = False
        self._token = None
        self._claimed_at = self._input_at = None
        self._seq = -1
        self._axes = dict.fromkeys(AXES, 0.)
        self._last_manual = self._last_gcs = None
        self._params = {}
        self._command = None
        self._phase = "idle" if enabled else "disabled"
        self._error = ""
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
            self._armed, self._mode = bool(base & 128), custom
            self._observe_command(at)
            if self._token and self._phase != "landing":
                self._phase = "armed" if self._armed else (
                    "prepared" if custom == ALT_HOLD else "claimed")
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
                self._landed = True if landed == 1 else False if landed in (2, 3, 4) else None
        elif event.type_name == "PARAM_VALUE" and self._claimed_at is not None:
            key = fields.get("param_id")
            if isinstance(key, bytes):
                key = key.decode("ascii", errors="replace")
            if isinstance(key, str):
                key = key.split("\0", 1)[0]
            value = fields.get("param_value")
            if (isinstance(key, str) and key in REQUIRED_PARAMETERS and _number(value)
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
                command["detail"] = f"Commande MAVLink refusée (résultat {result})"
                self._error = command["detail"]
                if command["action"] == "arm":
                    self._arm_uncertain = False
            elif not command["observed"]:
                command["state"] = "accepted"
                command["detail"] = "Accusé MAVLink reçu ; état du véhicule à confirmer"
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

    def _observe_command(self, at):
        command = self._command
        if (command is None or at < command["sent_at"]
                or command["state"] == "send_failed"):
            return
        action = command["action"]
        observed = ((action == "prepare" and self._mode == ALT_HOLD)
                    or (action == "arm" and self._armed)
                    or (action == "land" and self._mode == LAND)
                    or (action == "disarm" and self._armed is False))
        if observed:
            command["observed"] = True
            command["observed_at"] = at
            if command["state"] != "denied":
                command["state"] = "observed"
                command["detail"] = "État confirmé par le HEARTBEAT du véhicule"
            if action in ("arm", "disarm") or (action == "land" and not self._armed):
                # A buffered disarmed AltHold heartbeat after an unconfirmed arm
                # does not establish that arm failed. LAND + disarmed does show
                # the fallback mode, without treating its ACK as physical state.
                self._arm_uncertain = False

    def _unavailable(self, now):
        if not self.enabled:
            return "Pilotage désactivé ; observation uniquement"
        if self._closed or not self._link_available:
            return "Liaison de pilotage indisponible"
        if (self._heartbeat_at is None or now - self._heartbeat_at > HEARTBEAT_MAX_AGE
                or not self._identity_valid):
            return "HEARTBEAT ArduCopter récent attendu de la source sélectionnée"
        if self._simstate_at is None or now - self._simstate_at > SIMSTATE_MAX_AGE:
            return "Simulation non confirmée : SIMSTATE récent attendu"
        return ""

    def _profile(self):
        values = {key: entry[0] for key, entry in self._params.items()}
        missing = [key for key in REQUIRED_PARAMETERS if key not in values]
        mismatched = [key for key, expected in REQUIRED_PARAMETERS.items()
                      if key in values and not math.isclose(values[key], expected, abs_tol=1e-5)]
        return {"ready": not missing and not mismatched, "values": values,
                "required": dict(REQUIRED_PARAMETERS), "missing": missing,
                "mismatched": mismatched}

    def state(self, now):
        """Pure snapshot: reading status never refreshes input or vehicle receipts."""
        reason = self._unavailable(now)
        return {"at": now, "enabled": self.enabled, "available": not reason,
                "reason": reason, "owned": self._token is not None,
                "phase": self._phase, "last_input_age": _age(now, self._input_at),
                "input_timeout": INPUT_TIMEOUT, "axes": dict(self._axes),
                "vehicle": {"armed": self._armed, "mode": self._mode,
                            "landed": self._landed if self._landed_at is not None
                            and now - self._landed_at <= LANDED_MAX_AGE else None,
                            "heartbeat_age": _age(now, self._heartbeat_at)},
                "profile": self._profile(),
                "command": None if self._command is None else dict(self._command),
                "last_error": self._error}

    def _send(self, link, message, now):
        if link is None:
            return "closed", "Liaison MAVLink absente"
        try:
            result = link.send(message, now)
            return result.status.value, result.detail
        except Exception as exc:
            # Never retain the message for a later retry after a send exception.
            return "error", str(exc)

    def _manual(self, link, now, *, neutral=False):
        axes = dict.fromkeys(AXES, 0.) if neutral else self._axes
        if self._armed is not True:
            x = y = r = z = 0  # zero throttle before arm, regardless of UI input
        else:
            # Attitude/yaw input capped at 30%; vertical remains pilot-controlled.
            x, y, r = (round(axes[key] * 300) for key in ("forward", "right", "yaw"))
            z = round((axes["up"] + 1.) * 500)
        message = self._dialect().MAVLink_manual_control_message(self.system, x, y, z, r, 0)
        self._last_manual = now
        return self._send(link, message, now)

    def claim(self, link, now):
        if self._token is not None:
            raise RuntimeError("Le pilotage est déjà détenu par un navigateur")
        self._link_available = link is not None
        reason = self._unavailable(now)
        if reason:
            raise RuntimeError(reason)
        if self._armed is not False or self._arm_uncertain:
            raise RuntimeError("Attendez le désarmement confirmé avant de prendre le pilotage")
        self._token = secrets.token_urlsafe(24)
        self._claimed_at = self._input_at = now
        self._seq = -1
        self._axes = dict.fromkeys(AXES, 0.)
        self._params = {}
        self._command = None
        self._last_manual = self._last_gcs = None
        self._phase = "prepared" if self._mode == ALT_HOLD else "claimed"
        self._error = ""
        for key in REQUIRED_PARAMETERS:
            message = self._dialect().MAVLink_param_request_read_message(
                self.system, self.component, key.encode("ascii"), -1)
            status, detail = self._send(link, message, now)
            if status != "accepted":
                self._token = None
                self._phase = "error"
                self._error = detail or "Lecture des paramètres non transmise"
                raise RuntimeError(self._error)
        # Ask for HEARTBEAT and EXTENDED_SYS_STATE at 5 Hz. Sim time may run below
        # wall time; this keeps the same strict receipt-age bounds. A stream ACK
        # never stands in for an actual vehicle-state observation/action ACK.
        for message_id in (0, 245):
            interval = self._dialect().MAVLink_command_long_message(
                self.system, self.component, 511, 0, float(message_id), 200000., 0., 0., 0., 0., 0.)
            status, detail = self._send(link, interval, now)
            if status != "accepted":
                self._token = None
                self._phase = "error"
                self._error = detail or "Demande d’état du véhicule non transmise"
                raise RuntimeError(self._error)
        self.tick(link, now)
        return {"token": self._token, "control": self.state(now)}

    def _owner(self, token, link, now):
        # Expire before considering the request: an arriving delayed packet must
        # not resurrect input that already exceeded the deadman deadline.
        self._maintain(link, now)
        if not isinstance(token, str) or self._token is None or not secrets.compare_digest(token, self._token):
            raise RuntimeError("Pilotage absent, expiré ou détenu par un autre navigateur")

    def input(self, token, seq, axes, *, link, now):
        self._owner(token, link, now)
        if not _integer(seq, maximum=2**53 - 1) or seq <= self._seq:
            raise ValueError("La séquence des commandes doit être strictement croissante")
        if (not isinstance(axes, dict) or set(axes) != set(AXES)
                or any(not _number(value) or not -1. <= value <= 1.
                       for value in axes.values())):
            raise ValueError("Quatre axes finis compris entre -1 et 1 sont requis")
        self._seq = seq
        self._input_at = now
        self._axes = {key: float(axes[key]) for key in AXES}
        return self.state(now)

    def _pending(self):
        return self._command is not None and self._command["state"] in ("sent", "accepted")

    def _issue(self, action, link, now):
        command_id = DO_SET_MODE if action in ("prepare", "land") else ARM_DISARM
        params = ([1., float(ALT_HOLD if action == "prepare" else LAND)]
                  if command_id == DO_SET_MODE else [1. if action == "arm" else 0., 0.])
        message = self._dialect().MAVLink_command_long_message(
            self.system, self.component, command_id, 0, *params, 0., 0., 0., 0., 0.)
        status, detail = self._send(link, message, now)
        self._command = {"action": action, "command_id": command_id,
                         "sent_at": now, "transport": status,
                         "ack": None, "ack_at": None, "observed": False,
                         "observed_at": None,
                         "state": "sent" if status == "accepted" else "send_failed",
                         "detail": "Transmise localement ; confirmation attendue"
                         if status == "accepted" else detail or "Commande non transmise"}
        if action == "arm" and status in ("accepted", "partial", "error"):
            self._arm_uncertain = True
        if status != "accepted":
            self._error = self._command["detail"]
        return status == "accepted"

    def action(self, token, action, *, link, now):
        self._owner(token, link, now)
        if action not in ("prepare", "arm", "land", "disarm", "release"):
            raise ValueError("Action de pilotage inconnue")
        if action == "release":
            self._revoke(link, now, reason="Pilotage libéré", phase="released")
            return self.state(now)
        reason = self._unavailable(now)
        if reason:
            raise RuntimeError(reason)
        if action == "land":
            if self._phase == "landing":
                raise RuntimeError("L’atterrissage a déjà été demandé")
            self._axes = dict.fromkeys(AXES, 0.)
            self._manual(link, now, neutral=True)
            self._phase = "landing"
            if self._issue("land", link, now):
                self._error = ""
            return self.state(now)
        if self._pending():
            raise RuntimeError("Attendez la confirmation de la commande précédente")
        if action in ("prepare", "arm"):
            if any(self._axes.values()):
                raise RuntimeError("Relâchez toutes les commandes avant de préparer ou armer")
            if self._armed is not False or self._arm_uncertain:
                raise RuntimeError("Désarmement confirmé requis")
            if action == "arm":
                if not self._profile()["ready"]:
                    raise RuntimeError("Profil SITL sans GPS / repli non confirmé ; armement refusé")
                if self._mode != ALT_HOLD:
                    raise RuntimeError("Le mode AltHold doit être confirmé avant d’armer")
            status, detail = self._manual(link, now, neutral=True)
            if status != "accepted":
                self._error = detail or "Entrée neutre non transmise"
                raise RuntimeError(self._error)
            self._phase = "prepared" if self._mode == ALT_HOLD else "claimed"
        elif action == "disarm":
            if self._armed is False:
                raise RuntimeError("Le véhicule est déjà désarmé")
            if (self._landed is not True or self._landed_at is None
                    or now - self._landed_at > LANDED_MAX_AGE):
                raise RuntimeError("Désarmement refusé : état posé récent requis")
        if self._issue(action, link, now):
            self._error = ""
        return self.state(now)

    def _revoke(self, link, now, *, reason, phase):
        if self._token is None:
            return
        self._token = None
        self._axes = dict.fromkeys(AXES, 0.)
        self._phase = phase
        self._error = reason
        # Do not extend old motion while handing over to LAND. If the transport
        # is gone no send is possible; stopping heartbeat triggers SITL's profile.
        if link is not None and (self._armed is True or self._arm_uncertain):
            self._manual(link, now, neutral=True)
            if not (self._command and self._command["action"] == "land"):
                self._issue("land", link, now)

    def _maintain(self, link, now):
        self._link_available = link is not None
        command = self._command
        if command and command["state"] in ("sent", "accepted") and now - command["sent_at"] > COMMAND_TIMEOUT:
            command["state"] = "timeout"
            command["detail"] = "État du véhicule non confirmé avant expiration ; aucune répétition automatique"
            self._error = command["detail"]
        if self._token is not None:
            reason = self._unavailable(now)
            if reason:
                self._revoke(link, now, reason=reason, phase="error")
            elif now - self._input_at >= INPUT_TIMEOUT:
                self._revoke(link, now, reason="Commandes navigateur expirées ; pilotage libéré", phase="expired")
            elif self._armed is True and self._mode != ALT_HOLD and self._phase != "landing":
                self._revoke(link, now, reason="Mode changé hors du pilotage web", phase="released")
            elif self._armed is True and not self._profile()["ready"]:
                self._revoke(link, now, reason="Profil de simulation modifié pendant le pilotage", phase="error")

    def tick(self, link, now):
        if not self.enabled or self._closed:
            return
        self._maintain(link, now)
        if self._token is None:
            return
        if self._last_gcs is None or now - self._last_gcs >= HEARTBEAT_INTERVAL:
            message = self._dialect().MAVLink_heartbeat_message(6, 8, 0, 0, 4, 3)
            status, detail = self._send(link, message, now)
            self._last_gcs = now
            if status != "accepted":
                self._revoke(link, now, reason=detail or "HEARTBEAT GCS non transmis", phase="error")
                return
        if self._phase != "landing" and (
                self._last_manual is None or now - self._last_manual >= MANUAL_INTERVAL):
            status, detail = self._manual(link, now)
            if status != "accepted":
                self._revoke(link, now, reason=detail or "Commande manuelle non transmise", phase="error")

    def check_reconfigure(self):
        if self.enabled and (self._token is not None or self._armed is True or self._arm_uncertain):
            raise RuntimeError("Libérez le pilotage et attendez le désarmement avant de modifier les sources")

    def check_reconnect(self):
        if self._token is not None:
            raise RuntimeError("Libérez le pilotage avant de rouvrir la réception MAVLink")

    def begin_reconnect(self, now):
        """Resume only same-source reception; retain evidence of unresolved flight."""
        self.check_reconnect()
        self._recovery_at = now
        self._heartbeat_at = self._simstate_at = self._landed_at = None
        self._identity_valid = False
        self._landed = None
        self._link_available = False
        self._claimed_at = None
        self._params = {}

    def close(self, link, now):
        self._revoke(link, now, reason="Session de pilotage arrêtée", phase="released")
        self._closed = True
