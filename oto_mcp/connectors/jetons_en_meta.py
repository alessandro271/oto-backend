"""Retirer de `connector_credentials.meta` les clés de forme jeton : `oto-mcp maintenance
jetons-en-meta`.

`meta` ne porte que des satellites non secrets ; un jeton d'accès vit en mémoire, par
processus, et le refresh_token chiffré dans `secret_enc`. Des lignes d'avant portent
encore une de ces clés, en clair : celles du jeton d'accès Google (que le code rangeait
là jusqu'à ce lot), et des connecteurs retirés dont plus aucun code ne lit la ligne.

- La liste des clés est FERMÉE et partagée : `credentials_store.BEARER_META_KEYS`, la
  même que celle que les surfaces de statut masquent et que les relais ne recopient pas.
- **À blanc par défaut** : par connecteur et par clé, le nombre de lignes qui la portent ;
  rien n'est écrit. `appliquer=True` retire la clé (`meta - 'clé'`), et rien d'autre :
  aucune ligne supprimée, ni secret, ni autre satellite, ni `set_at` touchés.
- Seulement les connecteurs de la liste fermée `CONNECTEURS` (tous par défaut, ou une
  partie) ; un autre nom est refusé avant toute connexion.
- Une transaction bornée (`statement_timeout` 15 s, `lock_timeout` 5 s) ; après écriture,
  relecture dans une transaction neuve, et le rapport dit ce qui a été retiré ET ce qui
  reste. Idempotent : relancé, il ne trouve plus rien.
- **Jamais une valeur** : le rapport et le journal ne disent que connecteur, clé et nombre.

⚠️ Pour `google`, à lancer APRÈS le déploiement du code qui ne range plus le jeton dans
`meta`, l'ancienne couleur arrêtée : le code d'avant y réécrit le jeton à son prochain
renouvellement.
"""
from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger(__name__)

_BORNES = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")

#: Les connecteurs dont des lignes portent un jeton en clair dans `meta`. Liste FERMÉE :
#: la commande ne touche aucun autre connecteur (un satellite scellé comme le jar de
#: session du connecteur `api`, `session_jar_enc`, n'est de toute façon pas une clé de
#: la liste, et reste où il est).
CONNECTEURS = ("google", "memento", "atlassian", "folkmcp")


class ConnecteurHorsListe(ValueError):
    """Un connecteur nommé hors de `CONNECTEURS` : rien n'est lu ni écrit."""

_SQL_COMPTE = (
    "SELECT c.connector AS connecteur, k.cle, count(*) AS lignes "
    "FROM connector_credentials c CROSS JOIN unnest(%(cles)s::text[]) AS k(cle) "
    "WHERE c.connector = ANY(%(connecteurs)s) AND c.meta ? k.cle "
    "GROUP BY c.connector, k.cle")

_SQL_RETIRE = (
    "UPDATE connector_credentials SET meta = meta - %(cle)s::text "
    "WHERE connector = ANY(%(connecteurs)s) AND meta ? %(cle)s RETURNING connector")


def cles() -> tuple[str, ...]:
    from .. import credentials_store
    return credentials_store.BEARER_META_KEYS


def _borner(conn) -> None:
    for sql in _BORNES:
        conn.execute(sql)


def _compter(conn, connecteurs: list[str]) -> dict[str, dict[str, int]]:
    rows = conn.execute(_SQL_COMPTE, {"cles": list(cles()),
                                      "connecteurs": connecteurs}).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["connecteur"], {})[r["cle"]] = int(r["lignes"])
    return {c: dict(sorted(v.items())) for c, v in sorted(out.items())}


def nettoyer(*, appliquer: bool, connecteurs=CONNECTEURS) -> dict:
    """Le rapport : `{"applique", "connecteurs", "cles", ...}`, avec `a_retirer` (à blanc)
    ou `retire` et `reste` (appliqué), chacun `{connecteur: {clé: lignes}}`."""
    from ..db import _connect
    noms = sorted(set(connecteurs))
    hors_liste = [c for c in noms if c not in CONNECTEURS]
    if not noms or hors_liste:
        raise ConnecteurHorsListe(
            f"connecteurs permis : {', '.join(CONNECTEURS)} (reçu : "
            f"{', '.join(hors_liste) or 'aucun'})")
    rapport = {"applique": appliquer, "connecteurs": noms, "cles": list(cles())}
    with _connect() as conn:
        _borner(conn)
        avant = _compter(conn, noms)
        if not appliquer:
            return {**rapport, "a_retirer": avant}
        retire: dict[str, Counter] = {}
        for cle in cles():
            rows = conn.execute(_SQL_RETIRE, {"cle": cle, "connecteurs": noms}).fetchall()
            for r in rows:
                retire.setdefault(r["connector"], Counter())[cle] += 1
    for connecteur, par_cle in sorted(retire.items()):
        for cle, n in sorted(par_cle.items()):
            logger.info("jetons-en-meta : %s — clé %r retirée de %d ligne(s)",
                        connecteur, cle, n)
    with _connect() as conn:
        _borner(conn)
        reste = _compter(conn, noms)
    return {**rapport,
            "retire": {c: dict(sorted(v.items())) for c, v in sorted(retire.items())},
            "reste": reste}


def reste(rapport: dict) -> bool:
    """Vrai si, appliqué, la relecture trouve encore une clé à retirer."""
    return bool(rapport.get("reste"))


def _par_cle(comptes: dict[str, dict[str, int]], connecteur: str) -> str:
    return ", ".join(f"{cle} {n}" for cle, n in comptes.get(connecteur, {}).items()) \
        or "aucune"


def lignes_du_rapport(rapport: dict) -> list[str]:
    """Le rapport imprimé : une ligne par connecteur, chaque clé avec son nombre de lignes."""
    mode = "APPLIQUÉ" if rapport["applique"] else "À BLANC (rien n'est écrit ; --appliquer)"
    out = [f"jetons-en-meta : {mode} — clés : {', '.join(rapport['cles'])}"]
    for c in rapport["connecteurs"]:
        if not rapport["applique"]:
            out.append(f"  {c} : à retirer — {_par_cle(rapport['a_retirer'], c)}")
        else:
            out.append(f"  {c} : retiré — {_par_cle(rapport['retire'], c)} ; relu après "
                       f"coup, reste — {_par_cle(rapport['reste'], c)}")
    return out
