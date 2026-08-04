"""Traducteur MAVLink — le SEUL fichier du projet qui appelle `mav.*_send` pour piloter.

Tout ce qui est ici est vérifié dans la source ArduPilot (Copter 4.8-dev), pas
sur un forum : plusieurs posts racontent des choses fausses sur ce handler.

`GCS_MAVLink_Copter.cpp:890` `handle_message_set_attitude_target()` :
  - rejeté si le mode n'est pas guided (`in_guided_mode()`), donc mode 20 requis ;
  - `THROTTLE_IGNORE` posé              -> `hold_position()` et sortie ;
  - quaternion non unitaire à ±1e-3     -> `hold_position()` et sortie ;
  - les 3 rates sont tout-ou-rien : les 3 bits clairs (rates lus) ou les 3 posés
    (attitude seule). **Un mélange -> `hold_position()`.**
  - sémantique de `thrust` = bit 3 de `GUID_OPTIONS` (`SetAttitudeTarget_ThrustAsThrust`,
    valeur 8). À 0 (défaut) `thrust` est un **taux de montée** : 0,5 tient l'altitude,
    et ArduPilot ferme la boucle verticale au baro — sans GPS. C'est ce qu'on veut
    pour les barreaux 1-2. Le barreau 3 (CTBR) exige le bit à 1.
"""
import math

from pymavlink import mavutil

from .commands import AttitudeCmd, CtbrCmd
from .vehicle import VehicleBackend

_M = mavutil.mavlink

# Angles absolus seuls : on ignore les 3 rates, on garde attitude + poussée.
# = 0b00000111. Les bits THROTTLE_IGNORE et ATTITUDE_IGNORE restent clairs.
MASK_ANGLE = (_M.ATTITUDE_TARGET_TYPEMASK_BODY_ROLL_RATE_IGNORE
              | _M.ATTITUDE_TARGET_TYPEMASK_BODY_PITCH_RATE_IGNORE
              | _M.ATTITUDE_TARGET_TYPEMASK_BODY_YAW_RATE_IGNORE)

# CTBR : on ignore l'attitude, on fournit les 3 rates + la poussée. = 0b10000000.
MASK_RATES = _M.ATTITUDE_TARGET_TYPEMASK_ATTITUDE_IGNORE

GUID_OPTIONS_THRUST_AS_THRUST = 8      # bit 3 (mode.h:1207)


def quat_from_euler(roll: float, pitch: float, yaw: float) -> list[float]:
    """Euler NED (3-2-1) -> quaternion, à l'identique de `Quaternion::from_euler`
    (`AP_Math/quaternion.cpp`). Unitaire par construction : le handler refuse le
    message si la norme s'écarte de 1 de plus de 1e-3."""
    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)
    return [cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy]


class MavlinkBackend(VehicleBackend):
    """Le 3,5" (et le SITL/Gazebo) vus par la couche de décision."""

    name = "mavlink"

    def __init__(self, conn):
        self.m = conn
        self.sent = 0                  # compteur brut (le §1.5-C viendra s'y greffer)

    # ── setup du mode 20 ────────────────────────────────────────────────────
    def _param(self, name: bytes, value: float, ptype=_M.MAV_PARAM_TYPE_INT32):
        self.m.mav.param_set_send(self.m.target_system, self.m.target_component,
                                  name, value, ptype)

    def configure_nogps(self, timeout_s: float = 0.5):
        """Pose les deux paramètres dont dépend la sémantique de nos messages.

        `GUID_OPTIONS` = 0 : bit 3 clair -> `thrust` est un taux de montée (alt-hold
        baro). `GUID_TIMEOUT` bas : sans mise à jour, `angle_control_run()` remet
        l'attitude à plat au cap courant et annule la poussée (mode_guided.cpp:983).
        Filet de sécurité voulu — mais il faut qu'il se déclenche vite, pas au bout
        de 3 s (défaut), et il faut streamer plus vite que lui.
        """
        self._param(b"GUID_OPTIONS", 0)
        self._param(b"GUID_TIMEOUT", timeout_s, _M.MAV_PARAM_TYPE_REAL32)

    def set_mode(self, name: str) -> bool:
        mode = self.m.mode_mapping().get(name)
        if mode is None:
            return False
        self.m.set_mode(mode)
        return True

    def current_mode(self):
        return self.m.flightmode

    # ── les deux barreaux ───────────────────────────────────────────────────
    def send_attitude(self, cmd: AttitudeCmd, heading: float) -> None:
        """Barreau 1 : quaternion + `thrust` = taux de montée (0,5 = tenir l'alt).

        Le `dyaw` de la décision devient un cap absolu ICI, à partir du cap
        mesuré : la dérivée gyro du cap devient invisible, et le cap commandé
        reste par construction à `max_dyaw` du cap réel.
        """
        q = quat_from_euler(cmd.roll, cmd.pitch, heading + cmd.dyaw)
        self.m.mav.set_attitude_target_send(
            0, self.m.target_system, self.m.target_component,
            MASK_ANGLE, q, 0.0, 0.0, 0.0, cmd.thrust)
        self.sent += 1

    def send_ctbr(self, cmd: CtbrCmd) -> None:
        """Barreau 3 : rates de corps + poussée brute. Exige `GUID_OPTIONS` bit 3
        à 1, sinon la poussée sera relue comme un taux de montée — deux régimes
        de contrôle sans aucun message d'erreur."""
        self.m.mav.set_attitude_target_send(
            0, self.m.target_system, self.m.target_component,
            MASK_RATES, [1.0, 0.0, 0.0, 0.0],
            cmd.roll_rate, cmd.pitch_rate, cmd.yaw_rate, cmd.thrust)
        self.sent += 1
