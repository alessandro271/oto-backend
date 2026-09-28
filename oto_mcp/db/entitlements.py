"""Droits déclarés (ADR 0070 §7) : les poser, les retirer, les relire.

Le cœur ne sait pas qui paie. Un producteur (commerce, admin) POSE un droit sous son
étiquette `source` ; le cœur le RELIT à chaque usage, par le seul point de lecture
`access/entitlements.value_for`. Une ligne porte :

- une **portée**, trois : l'org (`org_id` posé, `sub` NULL), une personne dans l'org
  (les deux posés), une personne dans toutes ses orgs (`org_id` NULL, `sub` posé) —
  jamais ni l'un ni l'autre (`_portee`, et la contrainte `org_entitlements_une_portee`) ;
- une **clé du catalogue** (`entitlements_catalogue`) — toute autre est refusée ici ;
- une **valeur** entière, jamais vide (oui/non = 1/0, sinon un nombre) ;
- une **fenêtre** : début inclus, fin exclue, chaque borne nulle = non bornée.

Une ligne par (org, personne, droit, source) : reposer la remplace. La contrainte
`org_entitlements_une_ligne` (`UNIQUE NULLS NOT DISTINCT`) le tient, `sub` nul comme
`org_id` nul.

NON aplati dans la surface `db.*` (comme `outreach`) : `grant`, `revoke` sont trop
communs pour elle. Les appelants écrivent `from ..db import entitlements`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple, Optional

from .. import entitlements_catalogue as catalogue
from ._conn import _connect

# Le prédicat de vivacité À L'INSTANT DE LA BASE (`NOW()`), la même horloge pour tous les
# processus. Lu aussi par le témoin de la fin de droit (`db/unipile_fin_de_droit.py`).
_VIVANT = "starts_at <= NOW() AND (expires_at IS NULL OR expires_at > NOW())"
# Le même, à un instant donné (`%(t)s`, NULL = maintenant) : `value_for(now=…)`.
_VIVANT_A = ("starts_at <= COALESCE(%(t)s, NOW()) "
             "AND (expires_at IS NULL OR expires_at > COALESCE(%(t)s, NOW()))")

_COLONNES = ("org_id, sub, right_key, value, source, starts_at, expires_at, granted_by, "
             "granted_at")


def _portee(org_id: Optional[int], sub: Optional[str]) -> None:
    """Une ligne vaut pour une org, une personne, ou les deux — jamais pour personne."""
    if org_id is None and sub is None:
        raise ValueError("entitlement_scope_required: ni org ni personne — une ligne vaut "
                         "pour une org, une personne dans une org, ou une personne partout")


def grant(org_id: Optional[int], right_key: str, source: str, *, value: int,
          sub: Optional[str] = None, starts_at: Optional[datetime] = None,
          expires_at: Optional[datetime] = None,
          granted_by: Optional[str] = None) -> None:
    """Pose (upsert) le droit `right_key` sous l'étiquette `source`, pour l'org
    (`sub` omis), pour la personne `sub` dans l'org, ou pour la personne `sub` dans
    toutes ses orgs (`org_id` None). Idempotent.

    Rejouer remplace la ligne de CETTE (org, personne, droit, source), bornes comprises :
    le producteur dit l'état entier de son droit à chaque pose. `starts_at` omis =
    maintenant ; `expires_at` omis = sans échéance. Clé hors catalogue ou valeur
    vide / hors genre, ni org ni personne → `ValueError` nommée, rien n'est écrit."""
    _portee(org_id, sub)
    valeur = catalogue.valeur_valide(right_key, value)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO org_entitlements (org_id, sub, right_key, source, value, "
            "starts_at, expires_at, granted_by) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s) "
            "ON CONFLICT ON CONSTRAINT org_entitlements_une_ligne DO UPDATE SET "
            "value = EXCLUDED.value, starts_at = EXCLUDED.starts_at, "
            "expires_at = EXCLUDED.expires_at, granted_by = EXCLUDED.granted_by, "
            "granted_at = NOW()",
            (org_id, sub, right_key, source, valeur, starts_at, expires_at, granted_by),
        )


def revoke(org_id: Optional[int], right_key: str, source: str, *,
           sub: Optional[str] = None) -> bool:
    """Retire la ligne de CETTE (org, personne, droit, source) — les autres sources et
    les autres portées restent. `sub` omis = la ligne d'org SEULE (`sub IS NULL`),
    jamais une ligne par personne, même à droit et source identiques ; `org_id` None =
    la ligne de la personne partout SEULE, jamais une de ses lignes dans une org. True
    si une ligne a été supprimée."""
    _portee(org_id, sub)
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM org_entitlements WHERE org_id IS NOT DISTINCT FROM %s "
            "AND sub IS NOT DISTINCT FROM %s "
            "AND right_key = %s AND source = %s",
            (org_id, sub, right_key, source),
        ).rowcount
    return n > 0


class Posees(NamedTuple):
    """Les lignes VALIDES d'un droit, par portée : `org` = le maximum des lignes d'org
    (`sub` NULL), `personne` = le maximum des lignes de la personne (dans l'org et
    partout). `None` = aucune ligne valide de cette portée."""
    org: Optional[int]
    personne: Optional[int]


def valeurs_posees(org_id: Optional[int], sub: Optional[str], right_key: str,
                   now: Optional[datetime] = None) -> Posees:
    """Les lignes VALIDES à `now` (défaut : l'instant de la base) du droit `right_key`,
    toutes sources confondues, en UNE lecture : le maximum des lignes de l'org, et le
    maximum des lignes de la personne `sub` dans l'org ET partout. `org_id` None :
    seule la personne partout peut s'appliquer ; `sub` None : seule l'org. Les deux
    maxima restent SÉPARÉS : la règle qui les combine (le défaut d'instance ne répond
    qu'à l'org, la personne ne fait qu'ajouter) vit dans `access.entitlements`, seul
    lecteur."""
    _portee(org_id, sub)
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(value) FILTER (WHERE sub IS NULL) AS org, "
            "MAX(value) FILTER (WHERE sub IS NOT NULL) AS personne "
            "FROM org_entitlements WHERE right_key = %(cle)s "
            "AND ((org_id = %(org)s AND (sub IS NULL OR sub = %(sub)s)) "
            "     OR (org_id IS NULL AND sub = %(sub)s)) "
            f"AND {_VIVANT_A}",
            {"org": org_id, "cle": right_key, "sub": sub, "t": now},
        ).fetchone()
    return Posees(row["org"], row["personne"])


def list_for_org(org_id: int) -> list[dict[str, Any]]:
    """Toutes les lignes de l'org, portée org ET personnes dans l'org, échues et à venir
    comprises. Les lignes d'une personne partout (`org_id` NULL) n'y sont PAS : ce sont
    celles de `list_for_person_everywhere`.

    ⚠️ **Ne filtre PAS les dates** : une console doit voir un droit échu, sinon il
    devient invisible et donc irrécupérable."""
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT {_COLONNES} FROM org_entitlements WHERE org_id = %s "
            "ORDER BY right_key, sub NULLS FIRST, source",
            (org_id,),
        ).fetchall())


def list_for_person(sub: str, org_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Les lignes posées sur la personne `sub` (dans chacune de ses orgs et partout,
    celles-ci en dernier ; ou dans `org_id` seule), dates non filtrées. Les lignes d'org
    qui la couvrent aussi n'y sont PAS : ce sont celles de `list_for_org`."""
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT {_COLONNES} FROM org_entitlements WHERE sub = %s "
            "AND (%s::bigint IS NULL OR org_id = %s) ORDER BY org_id, right_key, source",
            (sub, org_id, org_id),
        ).fetchall())


def list_for_person_everywhere(sub: str) -> list[dict[str, Any]]:
    """Les lignes de la personne `sub` PARTOUT (`org_id` NULL) — ni ses lignes dans une
    org, ni les lignes d'org. Dates non filtrées, comme `list_for_org`."""
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT {_COLONNES} FROM org_entitlements WHERE org_id IS NULL AND sub = %s "
            "ORDER BY right_key, source",
            (sub,),
        ).fetchall())


def list_for_right(right_key: str, *, live_only: bool = True) -> list[dict[str, Any]]:
    """« Quelles orgs ou personnes ont ce droit ? » — les lignes de `right_key`, valides
    maintenant (`live_only`, défaut) ou toutes. Une ligne à valeur 0 y figure : elle dit
    « non » explicitement, ce qui est une réponse."""
    filtre = f" AND {_VIVANT}" if live_only else ""
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT {_COLONNES} FROM org_entitlements WHERE right_key = %s{filtre} "
            "ORDER BY org_id, sub NULLS FIRST, source",
            (right_key,),
        ).fetchall())
