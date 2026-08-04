"""L'interface véhicule — la frontière entre « ce qu'on veut » et « comment on le dit ».

Typée au niveau **CTBR**, pas attitude (PORTFOLIO §2.3, et c'est la décision qui
coûte cher si on la rate) : l'ACRO d'un whoop Betaflight est du *rate*, et une
politique entraînée en CTBR ne se brancherait pas sur une interface attitude.

    VehicleBackend.send_ctbr(...)       <- la primitive, plus petit dénominateur commun
      ├── mavlink : SET_ATTITUDE_TARGET, ATTITUDE_IGNORE + les 3 rates, GUID_OPTIONS bit 3
      └── crsf    : 4 canaux ACRO                                    (whoop, plus tard)
    VehicleBackend.send_attitude(...)   <- couche de confort, au-dessus

La loi de guidage écrite à la main consomme la couche attitude ; la politique
apprise consommera la primitive. Même backend, aucun fork.
"""
from abc import ABC, abstractmethod

from .commands import AttitudeCmd, CtbrCmd


class VehicleBackend(ABC):
    """Un véhicule pilotable. La SEULE couche autorisée à parler un protocole."""

    name = "?"

    @abstractmethod
    def send_ctbr(self, cmd: CtbrCmd) -> None:
        """Rates de corps + poussée collective. La primitive."""

    @abstractmethod
    def send_attitude(self, cmd: AttitudeCmd, heading: float) -> None:
        """Attitude + poussée. `heading` (rad) est le cap MESURÉ au moment de
        l'émission : c'est ici, et seulement ici, que le `dyaw` relatif devient
        un cap absolu. La décision ne manipule jamais de cap absolu."""
