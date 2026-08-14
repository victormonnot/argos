"""Composeur : fabriquer un message MAVLink arbitraire à la main (PORTFOLIO §1.3).

L'autre moitié de l'atelier. L'inspecteur regarde ce qui monte ; celui-ci fabrique
ce qui descend. Les deux dans le même outil, parce que le geste réel est un
aller-retour : je regarde ce qui passe, je tire un message, je regarde ce qui
revient.

**Le formulaire est construit depuis le dialecte, jamais écrit à la main.** Les
noms de champs, leurs types, leurs longueurs de tableau, leurs unités et leurs
énumérés sont tous portés par les classes générées. Un composeur avec une liste
de messages codée en dur serait faux le jour où le dialecte bouge — c'est-à-dire
tout de suite, puisqu'on vient d'en ajouter un.

⚠ **Ce que ce composeur n'est pas** : une porte de sortie. Les commandes qui
pilotent (§1.5-A) passent par `gate.py`, et une seule couche doit pouvoir les
émettre. Le composeur est un outil de diagnostic ; c'est l'appelant qui décide
de ce qu'il refuse d'émettre en vol (cf. `console.py`).
"""

# Champs qu'on ne veut pas voir dans un formulaire : ils sont imposés par la
# liaison, pas par l'opérateur.
AUTO = ("target_system", "target_component")


class ChampInvalide(ValueError):
    """Valeur qu'on n'a pas su convertir — avec le nom du champ fautif."""


def _classes(module):
    """{nom du message: classe générée} pour un dialecte."""
    return {c.msgname: c for c in module.mavlink_map.values()}


def _longueur_tableau(classe, nom):
    """0 si le champ est scalaire, N si c'est un tableau de N éléments.

    ⚠ `array_lengths` suit `ordered_fieldnames` (l'ordre du FIL, trié par taille
    décroissante), pas `fieldnames` (l'ordre de la DÉFINITION). Indexer avec le
    mauvais des deux donne des longueurs qui appartiennent à un autre champ, et
    ça ne se voit qu'à l'encodage."""
    try:
        return classe.array_lengths[classe.ordered_fieldnames.index(nom)]
    except (ValueError, IndexError, AttributeError):
        return 0


class Composer:
    """Catalogue des messages émettables + construction depuis des chaînes.

    `canaux` : {nom du canal: module de dialecte}. Un message n'appartient qu'à
    un canal — celui dont le dialecte le définit. C'est ce qui permet d'envoyer
    `ARGOS_TARGET` sur la liaison de désignation et `COMMAND_LONG` sur celle du
    drone sans que l'opérateur ait à y penser.
    """

    def __init__(self, canaux: dict):
        self.canaux = {k: v for k, v in canaux.items() if v is not None}

    # ── catalogue ───────────────────────────────────────────────────────────
    def catalogue(self) -> list:
        vus, out = set(), []
        for canal, module in self.canaux.items():
            for nom, classe in sorted(_classes(module).items()):
                if nom in vus:
                    continue                  # défini par deux dialectes -> le 1er gagne
                vus.add(nom)
                out.append({"nom": nom, "id": classe.id, "canal": canal,
                            "champs": self._champs(classe, module)})
        return out

    @staticmethod
    def _champs(classe, module) -> list:
        enums = getattr(classe, "fieldenums_by_name", {})
        unites = getattr(classe, "fieldunits_by_name", {})
        affichage = getattr(classe, "fielddisplays_by_name", {})
        champs = []
        for nom, type_ in zip(classe.fieldnames, classe.fieldtypes):
            champs.append({
                "nom": nom,
                "type": type_,
                "tableau": _longueur_tableau(classe, nom),
                "enum": enums.get(nom, ""),
                "unite": unites.get(nom, ""),
                "bitmask": affichage.get(nom) == "bitmask",
                "auto": nom in AUTO,
            })
        return champs

    # ── construction ────────────────────────────────────────────────────────
    def construire(self, nom: str, valeurs: dict):
        """Rend `(message, canal)`. Les valeurs arrivent en TEXTE (un formulaire
        HTML n'envoie rien d'autre) et sont converties selon le type déclaré."""
        for canal, module in self.canaux.items():
            classe = _classes(module).get(nom)
            if classe is None:
                continue
            args = {}
            for champ in self._champs(classe, module):
                brut = valeurs.get(champ["nom"], "")
                args[champ["nom"]] = self._convertir(champ, brut, module)
            return classe(**args), canal
        raise ChampInvalide(f"message inconnu de tous les dialectes : {nom}")

    @staticmethod
    def _convertir(champ, brut, module):
        nom, type_ = champ["nom"], champ["type"]
        brut = "" if brut is None else str(brut).strip()

        if type_.startswith("char") and champ["tableau"]:
            return brut.encode("utf-8")[:champ["tableau"]]

        if champ["tableau"]:
            morceaux = [m for m in brut.replace(";", ",").split(",") if m.strip()]
            vals = [Composer._scalaire(nom, type_, m, module) for m in morceaux]
            # On complète à la bonne longueur : un tableau court est refusé par
            # l'encodeur avec une erreur bien plus obscure que celle-ci.
            manque = champ["tableau"] - len(vals)
            if manque < 0:
                raise ChampInvalide(
                    f"{nom} : {len(vals)} valeurs pour un tableau de {champ['tableau']}")
            return vals + [0] * manque

        return Composer._scalaire(nom, type_, brut, module)

    @staticmethod
    def _scalaire(nom, type_, brut, module):
        if brut == "":
            return 0
        # Un énuméré peut s'écrire par son NOM. Taper MAV_CMD_COMPONENT_ARM_DISARM
        # au lieu de 400, c'est la différence entre un outil et un pense-bête :
        # les constantes vivent déjà dans le dialecte, autant s'en servir.
        if not brut.lstrip("+-").replace(".", "", 1).replace("x", "", 1).isalnum() \
                or brut[0].isalpha():
            valeur = getattr(module, brut, None)
            if valeur is None:
                raise ChampInvalide(f"{nom} : « {brut} » n'est ni un nombre ni une "
                                    "constante connue du dialecte")
            return valeur
        try:
            if type_.startswith(("float", "double")):
                return float(brut)
            return int(brut, 0)          # base 0 -> 0x… et 0b… acceptés tels quels
        except ValueError:
            raise ChampInvalide(f"{nom} : « {brut} » n'est pas un {type_} valide")
