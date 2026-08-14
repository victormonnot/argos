"""control — décision, garde-fous, et traduction vers un véhicule.

Trois couches strictement séparées (PORTFOLIO §1.5-A et §1.5-B) :

    guidance.py  -> DÉCIDE   : erreur pixel -> AttitudeCmd. Pur, testable, zéro MAVLink.
    gate.py      -> AUTORISE : porte de sortie UNIQUE. Aucune commande n'atteint le
                               véhicule sans passer ici (suivi, opérateur, politique
                               apprise plus tard). C'est la couture où ORCA/CBF-QP
                               se branchera en swarm.
    vehicle.py   -> TRADUIT  : interface typée au niveau CTBR ; mavlink_backend.py est
                               la SEULE implémentation aujourd'hui, crsf viendra pour
                               le whoop (§2.3).

À côté, deux modules qui ne décident ni ne commandent, ils OBSERVENT :

    link.py        -> mesure les liaisons (§1.5-C) : Hz, perte, débit, latence.
    designation.py -> publie ce que la perception voit, dans le dialecte maison
                      `ARGOS_TARGET` (§1.3). Sortant seulement : rien de ce qui
                      part de là ne peut faire bouger le drone.
"""
from .commands import AttitudeCmd, CtbrCmd, TargetView, VehicleState
from .gate import CommandGate, GateResult, Limits
from .guidance import GuidanceGains, VisualGuidance
from .vehicle import VehicleBackend

__all__ = [
    "AttitudeCmd", "CtbrCmd", "TargetView", "VehicleState",
    "CommandGate", "GateResult", "Limits",
    "GuidanceGains", "VisualGuidance",
    "VehicleBackend",
]
