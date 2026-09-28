"""Le périmètre d'un propriétaire : des orgs DÉCLARÉES, et ce qui s'en dérive.

On déclare des orgs ; les équipes, les comptes et les orgs PERSONNELLES de ces
comptes (`orgs.personal_of`, un espace privé mono-membre) s'en dérivent. Rien d'autre
ne se devine : un compte qui est AUSSI membre d'une org hors périmètre porte des
données (ses projets perso, ses jetons, son usage) que rien ne permet d'attribuer à
l'un plutôt qu'à l'autre — l'export REFUSE en le nommant (`ComptesPartages`), et
c'est une décision humaine qui le tranche, pas une règle.
"""
from __future__ import annotations

from dataclasses import dataclass


class PerimetreRefuse(RuntimeError):
    """Le périmètre déclaré ne se résout pas en un propriétaire sans ambiguïté."""


class ComptesPartages(PerimetreRefuse):
    def __init__(self, partages: dict[str, list[int]]):
        self.partages = partages
        detail = "; ".join(f"{s} (aussi membre de {o})" for s, o in sorted(partages.items()))
        super().__init__(f"{len(partages)} compte(s) du périmètre sont aussi membres "
                         f"d'orgs hors périmètre — à trancher avant tout export : {detail}")


@dataclass(frozen=True)
class Perimetre:
    orgs_declarees: tuple[int, ...]
    orgs: tuple[int, ...]          # déclarées + orgs personnelles de leurs comptes
    groupes: tuple[int, ...]
    subs: tuple[str, ...]

    def parametres(self) -> dict:
        return {
            "orgs": list(self.orgs),
            "orgs_txt": [str(o) for o in self.orgs],
            "groupes": list(self.groupes),
            "groupes_txt": [str(g) for g in self.groupes],
            "subs": list(self.subs),
        }


def resoudre(conn, orgs: list[int]) -> Perimetre:
    """Résout le périmètre des `orgs` déclarées, ou lève `PerimetreRefuse`."""
    declarees = tuple(sorted({int(o) for o in orgs}))
    if not declarees:
        raise PerimetreRefuse("aucune org déclarée : un périmètre se déclare, il ne se devine pas")
    connues = {r["id"] for r in conn.execute(
        "SELECT id FROM orgs WHERE id = ANY(%s)", (list(declarees),))}
    inconnues = [o for o in declarees if o not in connues]
    if inconnues:
        raise PerimetreRefuse(f"org(s) déclarée(s) introuvable(s) : {inconnues}")
    perso = [r["personal_of"] for r in conn.execute(
        "SELECT personal_of FROM orgs WHERE id = ANY(%s) AND personal_of IS NOT NULL",
        (list(declarees),))]
    groupes = tuple(r["id"] for r in conn.execute(
        "SELECT id FROM org_groups WHERE org_id = ANY(%s) ORDER BY id", (list(declarees),)))
    subs = tuple(sorted({r["sub"] for r in conn.execute(
        "SELECT sub FROM org_members WHERE org_id = ANY(%(o)s) "
        "UNION SELECT sub FROM org_group_members WHERE group_id = ANY(%(g)s)",
        {"o": list(declarees), "g": list(groupes)})} | set(perso)))
    personnelles = tuple(r["id"] for r in conn.execute(
        "SELECT id FROM orgs WHERE personal_of = ANY(%s) ORDER BY id", (list(subs),)))
    toutes = tuple(sorted(set(declarees) | set(personnelles)))
    partages: dict[str, list[int]] = {}
    for r in conn.execute(
            "SELECT sub, array_agg(org_id ORDER BY org_id) AS hors FROM org_members "
            "WHERE sub = ANY(%s) AND NOT (org_id = ANY(%s)) GROUP BY sub",
            (list(subs), list(toutes))):
        partages[r["sub"]] = list(r["hors"])
    if partages:
        raise ComptesPartages(partages)
    groupes = tuple(r["id"] for r in conn.execute(
        "SELECT id FROM org_groups WHERE org_id = ANY(%s) ORDER BY id", (list(toutes),)))
    return Perimetre(declarees, toutes, groupes, subs)
