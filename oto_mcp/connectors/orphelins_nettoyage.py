"""Retirer les références orphelines (#905) : `oto-mcp maintenance orphelins`.

`orphelins.constat()` liste les lignes qui NOMMENT un connecteur absent du catalogue
servi ; ce module les retire. Décision du 14/10/2026 : tout nettoyer, sauf les
credentials, qui partent plus tard, après un préavis à leurs propriétaires.

- **À blanc par défaut** : par table et par nom, ce qui serait retiré ; rien n'est écrit.
- `appliquer=True` :
  - SUPPRIME les lignes de `user_selected_connectors`, `connector_selection_removed` et
    `connector_availability` qui nomment un orphelin (une installation qui ne monte
    aucun outil, un retrait ou une activation de rien) ;
  - RETIRE ces noms du kit des orgs (`orgs.default_connectors`, `array_remove`) : les
    autres noms du kit restent, dans leur ordre.
- **Jamais les credentials ni leurs instances** sans `credentials=True`, refusé tant
  qu'une date de préavis ÉCHUE (strictement avant aujourd'hui, UTC) n'est pas donnée.
  Avec elle, un credential orphelin part par l'entonnoir du coffre
  (`credentials_store.clear_connector_credentials`), qui archive son instance dans la
  même transaction ; un pont remote (`meta.base_url`) n'est jamais touché.
- Les réglages (`connector_settings`) restent hors du nettoyage : lus et rapportés.

Le catalogue est celui que CE code sert (`orphelins.catalogue()`, le registre en
mémoire), jamais une liste écrite ici : lancé depuis un commit dont le registre diffère
de celui de la production, le rapport à blanc le montre (comparer avec le constat
servi, `oto_admin_monitoring op=connecteurs_orphelins`) avant toute écriture.

Chaque table a SA transaction courte (`statement_timeout` 15 s, `lock_timeout` 5 s) ;
après écriture, la table est relue dans une transaction neuve, et le rapport dit ce qui
a été retiré ET ce qui reste. Idempotent : relancé, il ne trouve plus rien. Chaque
écriture est journalisée (table, connecteur, nombre) : aucun secret, aucune valeur.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import Callable, Optional

from . import orphelins

logger = logging.getLogger(__name__)

_BORNES = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")

# Les lignes qui ne font qu'installer, retirer ou activer un nom : supprimées entières.
SUPPRIMEES = ("user_selected_connectors", "connector_selection_removed",
              "connector_availability")

_SQL_CONSTAT = dict(orphelins.REFERENCES)

# Un groupe (entité, connecteur) dont AUCUNE ligne n'est un pont remote : c'est le grain
# de `clear_connector_credentials`, qui retire tous les comptes du connecteur.
_CREDENTIALS_ORPHELINS = (
    "SELECT entity_type, entity_id, connector FROM connector_credentials "
    "WHERE connector <> ALL(%(catalogue)s) GROUP BY entity_type, entity_id, connector "
    "HAVING bool_and(COALESCE(meta->>'base_url', '') = '') "
    "ORDER BY connector, entity_type, entity_id")


class PreavisNonEchu(ValueError):
    """`credentials=True` sans date de préavis, ou avec une date pas encore échue."""


def verifier_preavis(preavis_echu: Optional[date], aujourd_hui: date) -> None:
    if preavis_echu is None:
        raise PreavisNonEchu(
            "--credentials exige --preavis-echu AAAA-MM-JJ : la date où le préavis aux "
            "propriétaires des clés a pris fin")
    if preavis_echu >= aujourd_hui:
        raise PreavisNonEchu(
            f"préavis au {preavis_echu.isoformat()} : pas encore échu (aujourd'hui "
            f"{aujourd_hui.isoformat()}, UTC) — les credentials restent")


def _borner(conn) -> None:
    for sql in _BORNES:
        conn.execute(sql)


def _compter(conn, table: str, catalogue: list[str]) -> dict[str, int]:
    rows = conn.execute(_SQL_CONSTAT[table], {"catalogue": catalogue}).fetchall()
    return {r["connecteur"]: int(r["lignes"]) for r in rows}


def _supprimer(table: str) -> Callable:
    def ecrire(conn, catalogue: list[str], _noms: list[str]) -> Counter:
        rows = conn.execute(
            f"DELETE FROM {table} WHERE connector <> ALL(%(catalogue)s) RETURNING connector",
            {"catalogue": catalogue}).fetchall()
        return Counter(r["connector"] for r in rows)
    return ecrire


def _retirer_du_kit(conn, _catalogue: list[str], noms: list[str]) -> Counter:
    """Un nom après l'autre, `array_remove` : les autres noms du kit ne bougent pas.
    Compte les orgs dont le kit a perdu ce nom."""
    retire: Counter = Counter()
    for nom in noms:
        cur = conn.execute(
            "UPDATE orgs SET default_connectors = array_remove(default_connectors, %s) "
            "WHERE %s = ANY(default_connectors)", (nom, nom))
        retire[nom] = cur.rowcount or 0
    return retire


def _retirer_les_credentials(conn, catalogue: list[str], _noms: list[str]) -> Counter:
    from .. import credentials_store
    retire: Counter = Counter()
    groupes = conn.execute(_CREDENTIALS_ORPHELINS, {"catalogue": catalogue}).fetchall()
    for g in groupes:
        retire[g["connector"]] += credentials_store.clear_connector_credentials(
            g["entity_type"], g["entity_id"], g["connector"], conn=conn)
    return retire


_ECRIVAINS: dict[str, Callable] = {
    **{t: _supprimer(t) for t in SUPPRIMEES},
    orphelins.KIT: _retirer_du_kit,
    orphelins.CREDENTIALS: _retirer_les_credentials,
}


def _traiter(table: str, catalogue: list[str], appliquer: bool) -> dict:
    from ..db import _connect
    with _connect() as conn:
        _borner(conn)
        avant = _compter(conn, table, catalogue)
        if not appliquer:
            return {"table": table, "a_retirer": avant}
        retire = _ECRIVAINS[table](conn, catalogue, sorted(avant)) if avant else Counter()
    for nom, n in sorted(retire.items()):
        logger.info("orphelins : %s — %d ligne(s) retirée(s) nommant %r, absent du "
                    "catalogue servi (#905)", table, n, nom)
    with _connect() as conn:
        _borner(conn)
        reste = _compter(conn, table, catalogue)
    return {"table": table, "retire": dict(sorted(retire.items())), "reste": reste}


def _lire(tables: list[str], catalogue: list[str]) -> list[dict]:
    from ..db import _connect
    with _connect() as conn:
        _borner(conn)
        return [{"table": t, "lignes": _compter(conn, t, catalogue)} for t in tables]


def nettoyer(*, appliquer: bool, credentials: bool = False,
             preavis_echu: Optional[date] = None,
             aujourd_hui: Optional[date] = None) -> dict:
    """Le rapport : `{"applique", "catalogue", "tables": [...], "intouchees": [...]}`.

    Une table traitée porte `a_retirer` (à blanc) ou `retire` et `reste` (appliqué),
    chacun `{connecteur: lignes}` ; une table hors du nettoyage porte ses `lignes`
    orphelines, lues seulement (avec `credentials`, les instances y sont relues après
    l'archivage que fait le coffre). Le refus du préavis est levé AVANT toute connexion."""
    if credentials:
        verifier_preavis(preavis_echu, aujourd_hui or datetime.now(timezone.utc).date())
    catalogue = sorted(orphelins.catalogue())
    traitees = [*SUPPRIMEES, orphelins.KIT] + ([orphelins.CREDENTIALS] if credentials else [])
    tables = [_traiter(t, catalogue, appliquer) for t in traitees]
    intouchees = [t for t, _ in orphelins.REFERENCES if t not in traitees]
    return {"applique": appliquer, "catalogue": len(catalogue), "tables": tables,
            "intouchees": _lire(intouchees, catalogue)}


def reste(rapport: dict) -> bool:
    """Vrai si une table APPLIQUÉE nomme encore un orphelin à la relecture."""
    return any(t.get("reste") for t in rapport["tables"])


def _noms(comptes: dict[str, int]) -> str:
    return ", ".join(f"{nom} {n}" for nom, n in sorted(comptes.items())) or "aucun"


def lignes_du_rapport(rapport: dict) -> list[str]:
    """Le rapport imprimé : une ligne par table, les noms avec leur nombre de lignes."""
    mode = "APPLIQUÉ" if rapport["applique"] else "À BLANC (rien n'est écrit ; --appliquer)"
    out = [f"orphelins : {mode} — catalogue servi par ce code : {rapport['catalogue']} "
           "connecteurs"]
    for t in rapport["tables"]:
        if "a_retirer" in t:
            out.append(f"  {t['table']} : à retirer — {_noms(t['a_retirer'])}")
        else:
            out.append(f"  {t['table']} : retiré — {_noms(t['retire'])} ; "
                       f"relu après coup, reste — {_noms(t['reste'])}")
    for t in rapport["intouchees"]:
        out.append(f"  {t['table']} : lue seulement — {_noms(t['lignes'])}")
    return out
