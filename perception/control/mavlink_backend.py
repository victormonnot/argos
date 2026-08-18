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
import time

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

# ── override RC : le substitut SITL de la liaison ELRS (HITL-2) ──────────────
# `RCMAP_*` par défaut d'ArduPilot. Ce sont des NUMÉROS DE FONCTION, pas de
# canaux physiques : si un jour RCMAP change côté firmware, c'est ici que ça se
# corrige, et nulle part ailleurs.
RC_ROLL, RC_PITCH, RC_THROTTLE, RC_YAW = 1, 2, 3, 4
RC_NEUTRE, RC_SPAN = 1500, 500         # µs ; 1000..2000 est la plage RC standard
RC_INCHANGE = 65535                    # « ne touche pas à ce canal » (UINT16_MAX)
# Manche de tangage vers l'avant = PWM BAS = nez bas = on avance. C'est la
# convention RC habituelle, mais elle dépend de `RC2_REVERSED` : à vérifier une
# fois en vol, et c'est le seul signe de ce fichier qui ne soit pas déductible
# de la source ArduPilot.
RC_PITCH_INVERSE = True


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
        self.sent = 0                  # commandes de vol émises
        self._ts1 = None               # jeton de la requête TIMESYNC en cours

    # ── mesure de latence (§1.5-C) ──────────────────────────────────────────
    def ping(self):
        """Envoie une requête `TIMESYNC` et retient son jeton.

        C'est le seul aller-retour que le protocole offre sans rien inventer :
        ArduPilot répond en renvoyant notre `ts1` tel quel, avec son propre temps
        dans `tc1` (`GCS_Common.cpp`, `handle_timesync`). On mesure donc un vrai
        aller-retour applicatif — pas un ping ICMP, qui ne dirait rien de la file
        d'attente MAVLink ni de la charge du firmware.

        (`PING` n'existe plus dans ce firmware : vérifié, aucun handler.)
        """
        self._ts1 = time.time_ns()
        self.m.mav.timesync_send(0, self._ts1)

    def pong(self, msg):
        """Rend l'aller-retour en ms si `msg` est la réponse à NOTRE requête.

        Le filtre compte : `tc1 != 0` distingue une réponse d'une requête, et
        comparer `ts1` évite de mesurer l'écho d'une requête émise par quelqu'un
        d'autre sur la même liaison (QGC en fait aussi)."""
        if self._ts1 is None or msg.tc1 == 0 or msg.ts1 != self._ts1:
            return None
        rtt = (time.time_ns() - self._ts1) / 1e6
        self._ts1 = None
        return rtt

    @property
    def bytes_sent(self):
        """Total d'octets écrits sur la liaison, compteur de pymavlink."""
        return self.m.mav.total_bytes_sent

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

    def arm(self, armer: bool) -> None:
        """Armement / désarmement — une commande PONCTUELLE, pas un flux.

        Elle ne traverse pas `CommandGate` et c'est volontaire : la porte de
        sortie gouverne les **consignes d'attitude**, pas l'état des moteurs.
        Écrêter un armement n'a aucun sens, et l'y faire passer diluerait ce que
        la porte garantit.

        On n'utilise **pas** le désarmement forcé (`param2 = 21196`) : ArduPilot
        refuse de désarmer en vol, et ce refus est un bon comportement. Il revient
        en `COMMAND_ACK` — donc visible dans l'atelier MAVLink.
        """
        self.m.mav.command_long_send(self.m.target_system, self.m.target_component,
                                     _M.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                                     1 if armer else 0, 0, 0, 0, 0, 0, 0)

    # ── le chemin PILOTE : override RC (HITL-2) ─────────────────────────────
    def send_rc(self, canaux: dict) -> None:
        """Émet un `RC_CHANNELS_OVERRIDE`. Les canaux absents restent INCHANGÉS.

        Un seul message porte les canaux de pilotage (1-4) et ceux de la charge
        utile (6-8, le gimbal) : ce sont les mêmes huit champs. D'où le passage
        par un dictionnaire — l'appelant ne remplit que ce qu'il possède, et
        `RC_INCHANGE` protège le reste.

        ⚠ Cet override **expire** au bout de `RC_OVERRIDE_TIME` (3 s par défaut)
        côté ArduPilot : il faut le réémettre en continu, sans quoi le firmware
        reprend les vraies entrées RC — inexistantes en SITL, donc failsafe.
        """
        vals = [canaux.get(i, RC_INCHANGE) for i in range(1, 9)]
        self.m.mav.rc_channels_override_send(
            self.m.target_system, self.m.target_component, *vals)
        # `sent` compte les commandes de VOL. Un override qui ne porte que le
        # gimbal est une commande de charge utile et n'en est pas une ; celui qui
        # porte les manches, si. Sans cette distinction le compteur du HUD
        # tomberait a zero pendant tout un vol piloté à la main.
        if any(c in canaux for c in (RC_ROLL, RC_PITCH, RC_THROTTLE, RC_YAW)):
            self.sent += 1

    @staticmethod
    def rc_pwm(v: float) -> int:
        """-1..1 -> 1000..2000 µs, écrêté. La conversion vit ici parce que c'est
        du protocole : la couche décision ne connaît pas les microsecondes."""
        v = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
        return int(round(RC_NEUTRE + RC_SPAN * v))

    @classmethod
    def rc_manches(cls, avance: float, droite: float, lacet: float,
                   gaz: float) -> dict:
        """Les quatre manches -> les quatre canaux de fonction d'ArduPilot.

        C'est la seule traduction du projet qui parle en microsecondes, et elle
        est ici pour la même raison que `send_attitude` : un seul fichier encode.
        """
        return {
            RC_ROLL: cls.rc_pwm(droite),
            RC_PITCH: cls.rc_pwm(-avance if RC_PITCH_INVERSE else avance),
            RC_THROTTLE: cls.rc_pwm(gaz),
            RC_YAW: cls.rc_pwm(lacet),
        }

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
