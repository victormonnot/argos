"""La porte de sortie UNIQUE (PORTFOLIO §1.5-A, contrainte C du swarm).

Avant : la garde stop-when-close était un `if` **à l'intérieur** de la branche de
suivi ; la branche de vol manuel ne la traversait pas. La sécurité ne couvrait
donc que la moitié des chemins — et un opérateur pouvait pousser le drone dans
la cible que le suivi, lui, refusait d'approcher.

Maintenant : **aucune commande n'atteint le véhicule sans passer par
`CommandGate.submit()`.** Suivi, opérateur, et demain politique apprise. Le
backend est privé de la porte ; personne d'autre ne l'appelle.

C'est aussi, littéralement, la couture où ORCA / CBF-QP se branchera en swarm :
un filtre de sécurité qui prend une commande désirée et en rend une sûre.
"""
import math
import time
from dataclasses import dataclass, field

from .commands import AttitudeCmd, TargetView, VehicleState
from .vehicle import VehicleBackend

DEG = math.pi / 180.0


@dataclass
class Limits:
    """Les bornes dures. Ce ne sont pas les gains de la loi : la loi peut être
    fausse, ces bornes tiennent quand même."""
    max_tilt: float = 15.0 * DEG    # inclinaison absolue autorisée, tous chemins
    max_dyaw: float = 8.0 * DEG     # delta de cap par cycle
    thrust_min: float = 0.30        # 0,5 = tenir l'alt -> on borne la descente
    thrust_max: float = 0.70        # ... et la montée
    size_stop: float = 0.12         # bbox >= : plus AUCUN piqué, quel qu'en soit
                                    # l'émetteur (c'est LA garde de proximité).
                                    # À tenir aligné sur `GuidanceGains.size_near`,
                                    # et c'est une calibration géométrique — voir
                                    # l'avertissement en tête de guidance.py.


@dataclass
class GateResult:
    sent: bool
    cmd: AttitudeCmd
    reasons: list = field(default_factory=list)


class CommandGate:
    def __init__(self, backend: VehicleBackend, limits: Limits | None = None):
        self._backend = backend
        self.lim = limits or Limits()
        self.last_sent = 0.0
        self.last: GateResult = GateResult(False, AttitudeCmd(), ["jamais émis"])
        self.blocked = 0                # nombre de commandes écrêtées par la garde

    def _clamp(self, v, lo, hi):
        return lo if v < lo else (hi if v > hi else v)

    def submit(self, cmd: AttitudeCmd, state: VehicleState,
               target: TargetView | None = None) -> GateResult:
        """Le seul chemin vers le véhicule. Rend la commande RÉELLEMENT émise."""
        lim, reasons = self.lim, []

        if not state.flying:
            self.last = GateResult(False, cmd, ["pas en vol"])
            return self.last

        roll = self._clamp(cmd.roll, -lim.max_tilt, lim.max_tilt)
        pitch = self._clamp(cmd.pitch, -lim.max_tilt, lim.max_tilt)
        dyaw = self._clamp(cmd.dyaw, -lim.max_dyaw, lim.max_dyaw)
        thrust = self._clamp(cmd.thrust, lim.thrust_min, lim.thrust_max)
        if (roll, pitch, dyaw) != (cmd.roll, cmd.pitch, cmd.dyaw):
            reasons.append("écrêté")

        # Garde de proximité — la seule règle vraiment métier de cette porte, et
        # elle s'applique à TOUS les émetteurs. Piqué = pitch négatif = avance ;
        # on ne le supprime que dans ce sens, cabrer pour freiner reste permis.
        if target is not None and target.has and target.size >= lim.size_stop:
            if pitch < 0.0:
                pitch = 0.0
                reasons.append(f"proximité ({target.size:.2f})")
                self.blocked += 1

        out = cmd.replace(roll=roll, pitch=pitch, dyaw=dyaw, thrust=thrust)
        self._backend.send_attitude(out, state.heading)
        self.last_sent = time.time()
        self.last = GateResult(True, out, reasons)
        return self.last

    def hold(self, state: VehicleState) -> GateResult:
        """Rester à plat, cap et altitude tenus. Émis en continu quand personne
        ne commande : ne jamais laisser `GUID_TIMEOUT` expirer par inadvertance."""
        return self.submit(AttitudeCmd(source="idle"), state)
