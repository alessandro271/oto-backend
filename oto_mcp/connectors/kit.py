"""Le kit d'organisation — UNE fonction d'application (ADR 0050 §E8, oto#166).

Le kit (`orgs.default_connectors`) est la liste de connecteurs qu'une org installe
dans la boîte à outils de ses membres — ceux qui arrivent (au semis,
`session_visibility`) ET ceux qui sont déjà là (ici, au geste de l'admin). Tout
geste d'org sur ces boîtes passe par `appliquer` :

| geste (capacité)                                   | appel                              |
|----------------------------------------------------|------------------------------------|
| poser le kit en entier (`connectors.recommend`)    | `appliquer(org, kit=[…])`          |
| ajouter au kit (`connectors.bulk_select`)          | `appliquer(org, ajouter=[nom])`    |
| retirer du kit (`connectors.unset_default`)        | `appliquer(org, retirer=[nom])`    |

Seule la DIFFÉRENCE entre l'ancien et le nouveau kit s'applique aux membres
(décision Q4 du 11/09 : « une modification future s'applique à tous les membres,
anciens compris ; ce qui a été posé avant n'est pas rejoué »). Un connecteur nommé
par le geste mais déjà au kit n'est rejoué chez personne, et la réponse le dit
(`unchanged`).

Un ajout installe chez chaque membre de l'org, provenance `kit` (§E4), par
`selection.install_for_member` — jamais par-dessus le membre : une ligne existante
(active ou en pause) reste, un retrait du membre n'est pas défait. Le kit et les
boîtes s'écrivent dans UNE transaction, la ligne de l'org verrouillée (`FOR
UPDATE`) : deux admins qui modifient le kit en même temps ne calculent pas leur
différence sur le même « avant ».

La réponse est chiffrée par connecteur : installé chez N, déjà actif chez M, laissé
chez P qui l'ont en pause, laissé chez R qui l'ont retiré eux-mêmes, dont K qu'une
restriction d'accès masque. Visible à l'écran tout de suite ; pour l'agent d'un
membre, à sa PROCHAINE conversation — le registre d'outils est figé à l'ouverture.
"""
from __future__ import annotations

from typing import Iterable, Optional

from . import selection as sel

ADDED = "added"
REMOVED = "removed"

AGENT_NOTE = ("Effet visible à l'écran tout de suite ; pour l'agent d'un membre, à sa "
              "PROCHAINE conversation — le registre d'outils d'une conversation ouverte "
              "est figé et aucune écriture n'y change rien.")
# Servi à l'ADMIN quand son geste nomme un connecteur déjà au kit (lecture de Q4
# retenue le 11/09/2026, ADR 0050 §E) : son clic a bien été reçu, il n'y avait rien à
# modifier. Le texte dit POURQUOI et COMMENT faire s'il le veut vraiment chez les
# membres actuels — une décision de l'admin, pas un rattrapage de la plateforme. Il
# est vrai avant comme après l'application de Q1 : un retrait du kit ne désinstalle
# jamais que ce que le kit a lui-même posé.
UNCHANGED_NOTE = (
    "Déjà dans le kit : ton geste a bien été reçu, mais il ne modifie pas le kit, donc "
    "il n'installe rien chez les membres actuels. Le kit n'applique aux membres déjà "
    "entrés que ses MODIFICATIONS faites depuis le 11/09/2026 ; ce qu'il contenait avant "
    "ne se rejoue pas. Pour l'installer malgré tout chez les membres actuels : retire-le "
    "du kit, puis remets-le. Ce retrait ne désinstalle rien que le kit n'ait posé "
    "lui-même ; la remise l'installe chez chaque membre qui ne l'a pas — sauf chez qui "
    "l'a retiré lui-même depuis le 11/09/2026, qui le garde retiré.")


class OrgInconnue(LookupError):
    """L'org visée n'existe pas."""


def _dedupe(noms: Iterable[str]) -> list[str]:
    vus: set[str] = set()
    out: list[str] = []
    for n in noms:
        if n not in vus:
            vus.add(n)
            out.append(n)
    return out


def _masques(org_id: int, connecteur: str, subs: list[str]) -> Optional[int]:
    """Parmi `subs` (ceux chez qui le connecteur vient d'être posé), combien une
    restriction d'accès masque — les deux paliers que `compute_hidden_tools`
    applique (org, ADR 0025 ; équipe active, ADR 0012). `None` = non calculé (une
    lecture a échoué) : jamais un zéro qu'on n'a pas mesuré."""
    from .. import access
    try:
        n = 0
        for s in subs:
            if (connecteur in access.rbac_denied_connectors(s, org_id)
                    or connecteur in access.group_rbac_denied_connectors(
                        s, access.current_group(s))):
                n += 1
        return n
    # noqa: SILENT — un compteur non calculé se DIT (None), il ne se devine pas
    except Exception:
        return None


def appliquer(org_id: int, *, kit: Optional[Iterable[str]] = None,
              ajouter: Iterable[str] = (), retirer: Iterable[str] = ()) -> dict:
    """Applique un geste d'org sur le kit et les boîtes de ses membres. Voir le module."""
    from .. import db

    ajouter, retirer = _dedupe(ajouter), _dedupe(retirer)
    if kit is not None and (ajouter or retirer):
        raise ValueError("appliquer : `kit` (le kit entier) OU `ajouter`/`retirer`, pas les deux")
    with db._connect() as conn:
        row = conn.execute("SELECT default_connectors FROM orgs WHERE id = %s FOR UPDATE",
                           (org_id,)).fetchone()
        if row is None:
            raise OrgInconnue(org_id)
        avant = list(row["default_connectors"] or [])
        if kit is not None:
            nommes = _dedupe(kit)
            apres = nommes
        else:
            nommes = ajouter + retirer
            apres = [n for n in avant if n not in set(retirer)] + [
                n for n in ajouter if n not in avant]
        ajouts = [n for n in apres if n not in avant]
        retraits = [n for n in avant if n not in apres]
        if ajouts or retraits or (kit is not None and row["default_connectors"] is None):
            conn.execute("UPDATE orgs SET default_connectors = %s WHERE id = %s",
                         (apres, org_id))
        membres = [r["sub"] for r in conn.execute(
            "SELECT sub FROM org_members WHERE org_id = %s ORDER BY joined_at, sub",
            (org_id,)).fetchall()]
        effets: list[dict] = []
        poses: dict[str, list[str]] = {}
        for c in ajouts:
            comptes = {"installed": 0, "already_active": 0, "paused": 0,
                       "removed_by_member": 0}
            poses[c] = []
            for m in membres:
                issue = sel.install_for_member(conn, m, c, org_id, sel.KIT)
                comptes[issue] += 1
                if issue == "installed":
                    poses[c].append(m)
            effets.append({"connector": c, "change": ADDED, **comptes})
        for c in retraits:
            # E5 d'avant la décision Q1 : un retrait du kit ne désinstalle chez
            # personne. On COMPTE ce qui reste, par provenance, pour que la réponse le
            # dise au lieu de le taire (le barreau 4 applique Q1).
            reste = {r["origin"]: int(r["n"]) for r in conn.execute(
                "SELECT origin, count(*) AS n FROM user_selected_connectors "
                "WHERE org_id = %s AND connector = %s AND sub = ANY(%s) GROUP BY origin",
                (org_id, c, membres)).fetchall()}
            effets.append({"connector": c, "change": REMOVED, "uninstalled": 0,
                           "kept": reste})
    for e in effets:
        if e["change"] == ADDED:
            e["masked_by_access"] = _masques(org_id, e["connector"], poses[e["connector"]])
    unchanged = [n for n in nommes if n not in ajouts and n not in retraits]
    out = {"org_id": org_id, "kit": apres, "members": len(membres),
           "changes": effets, "unchanged": unchanged, "note": AGENT_NOTE}
    if unchanged:
        out["unchanged_note"] = UNCHANGED_NOTE
    return out
