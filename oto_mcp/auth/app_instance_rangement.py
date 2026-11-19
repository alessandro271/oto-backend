"""Seal into the vault the instance-app secrets still stored in plain in `connector_settings`
— `oto-mcp maintenance secrets-d-application`.

The readers (`auth/app_instance`) read a secret from the vault only; `connector_settings`
refuses a new one. What remains is the rows written before: this module moves them.
A revision cannot: sealing needs the master key (same reason as `copies-du-coffre`).

- **Dry run by default**: per row, connector, key and state — never a value.
- `appliquer=True`: for each secret-shaped PLATFORM row (`a_la_forme_d_un_secret`), in ONE
  bounded transaction (`statement_timeout` 15 s, `lock_timeout` 5 s): seal it into the
  vault (`credentials_store.set_app_secret`, same transaction), read it back and check it
  decrypts to the same value (compared, never printed), then DELETE the plain row. Any
  failure rolls the row back whole: never a plain row erased without its sealed copy.
- `effacer=False` (`--sans-effacer`): seal and check, keep the plain row — the first
  phase of the rollout, while the code still served reads `connector_settings`
  (`docs/commands.md` §secrets-d-application).
- Idempotent: a secret already in the vault with the same value is "already in the
  vault" (its plain row erased when `effacer`); one with ANOTHER value is a gap — left
  untouched, reported, exit code 1: deciding which one is right is the operator's call.
- An OLD card (`sharepoint`, whose coordinates moved to the `microsoft` carrier) is
  sealed under its carrier only if the carrier has no value of its own; otherwise its
  plain row is a stale copy nobody reads, erased with `effacer`.
- A secret-shaped row outside the PLATFORM scope is not an instance-app secret: reported,
  never touched (exit code 1).
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import credentials_store
from ..db import connector_settings as store

logger = logging.getLogger(__name__)

_BORNES = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")

#: Old card → the carrier its application's coordinates moved to (`auth/microsoft`).
CARTES_DEPLACEES = {"sharepoint": "microsoft"}

#: Who the vault records as having set a moved secret.
AUTEUR = "migration:connector_settings"

# The states of a row (what is printed, with connector and key).
A_DEPLACER = "à déplacer"
DEPLACE = "déplacé"
COPIE = "copié au coffre, clair conservé"
DEJA = "déjà au coffre"
DEJA_EFFACE = "déjà au coffre, clair effacé"
PERIME = "copie périmée d'une ancienne carte"
PERIME_EFFACE = "copie périmée d'une ancienne carte, effacée"
ECART = "écart : le coffre porte une autre valeur — rien n'est touché"
HORS_PLATEFORME = "hors palier plateforme — rien n'est touché"

#: The states that leave the operator something to decide.
REFUS = frozenset({ECART, HORS_PLATEFORME})


class RelectureEnEchec(RuntimeError):
    """The sealed secret does not read back to the value it was sealed from."""


def _borner(conn) -> None:
    for sql in _BORNES:
        conn.execute(sql)


def _lignes(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT scope_type, scope_id, connector, key, value FROM connector_settings "
        "ORDER BY scope_type, scope_id, connector, key").fetchall()
    return [dict(r) for r in rows
            if store.a_la_forme_d_un_secret(r["connector"], r["key"])]


def _ordre(ligne: dict) -> tuple:
    """Carriers before old cards: an old card is judged against what its carrier holds."""
    return (ligne["connector"] in CARTES_DEPLACEES, ligne["connector"], ligne["key"])


def _effacer(conn, ligne: dict) -> None:
    conn.execute(
        "DELETE FROM connector_settings WHERE scope_type = %s AND scope_id = %s "
        "AND connector = %s AND key = %s",
        (ligne["scope_type"], ligne["scope_id"], ligne["connector"], ligne["key"]))


def _valeur_du_porteur(conn, cible: str, key: str) -> Optional[str]:
    """What the carrier holds for `key`: in the vault, else its own plain row."""
    au_coffre = credentials_store.get_app_secrets(cible, [key], conn=conn).get(key)
    if au_coffre:
        return au_coffre
    row = conn.execute(
        "SELECT value FROM connector_settings WHERE scope_type = 'platform' "
        "AND connector = %s AND key = %s", (cible, key)).fetchone()
    return ((row["value"] or "").strip() or None) if row else None


def _traiter(conn, ligne: dict, appliquer: bool, effacer: bool) -> str:
    connecteur, key = ligne["connector"], ligne["key"]
    valeur = (ligne["value"] or "").strip()
    if ligne["scope_type"] != "platform":
        return HORS_PLATEFORME
    cible = CARTES_DEPLACEES.get(connecteur, connecteur)
    if cible != connecteur and _valeur_du_porteur(conn, cible, key):
        if appliquer and effacer:
            _effacer(conn, ligne)
            return PERIME_EFFACE
        return PERIME
    au_coffre = credentials_store.get_app_secrets(cible, [key], conn=conn).get(key)
    if au_coffre is not None:
        if au_coffre != valeur:
            return ECART
        if appliquer and effacer:
            _effacer(conn, ligne)
            return DEJA_EFFACE
        return DEJA
    if not valeur:
        # An empty plain row seals nothing: it is erased like a moved one.
        if appliquer and effacer:
            _effacer(conn, ligne)
            return DEPLACE
        return A_DEPLACER
    if not appliquer:
        return A_DEPLACER
    credentials_store.set_app_secret(cible, key, valeur, set_by=AUTEUR, conn=conn)
    relu = credentials_store.get_app_secrets(cible, [key], conn=conn).get(key)
    if relu != valeur:
        raise RelectureEnEchec(
            f"{cible}.{key} : le secret scellé ne se relit pas à l'identique — rien "
            f"n'est écrit pour cette ligne")
    if effacer:
        _effacer(conn, ligne)
        return DEPLACE
    return COPIE


def ranger(*, appliquer: bool, effacer: bool = True) -> dict:
    """The report: `{"applique", "effacer", "lignes": [{connector, key, scope, etat}]}`.

    One bounded transaction per row: what is done stays done if a later row fails."""
    from ..db import _connect

    with _connect() as conn:
        _borner(conn)
        cles = sorted(({k: r[k] for k in ("scope_type", "scope_id", "connector", "key")}
                       for r in _lignes(conn)), key=_ordre)
    lignes = []
    for cle in cles:
        with _connect() as conn:
            _borner(conn)
            row = conn.execute(
                "SELECT scope_type, scope_id, connector, key, value FROM connector_settings "
                "WHERE scope_type = %s AND scope_id = %s AND connector = %s AND key = %s"
                + (" FOR UPDATE" if appliquer else ""),
                (cle["scope_type"], cle["scope_id"], cle["connector"], cle["key"])
            ).fetchone()
            if row is None:            # erased meanwhile (an old card, a concurrent run)
                continue
            etat = _traiter(conn, dict(row), appliquer, effacer)
        if etat in (DEPLACE, COPIE, DEJA_EFFACE, PERIME_EFFACE):
            logger.info("secrets-d-application : %s.%s — %s", cle["connector"], cle["key"],
                        etat)
        lignes.append({"connector": cle["connector"], "key": cle["key"],
                       "scope": cle["scope_type"], "etat": etat})
    return {"applique": appliquer, "effacer": effacer, "lignes": lignes}


def refus(rapport: dict) -> bool:
    """A row the command will not decide (a gap, a non-platform secret)."""
    return any(r["etat"] in REFUS for r in rapport["lignes"])


def reste(rapport: dict) -> bool:
    """Something remains to do: a secret not in the vault yet, or — when erasing — a
    plain row still there."""
    a_faire = {A_DEPLACER}
    if rapport["effacer"]:
        a_faire |= {DEJA, PERIME}
    return any(r["etat"] in a_faire for r in rapport["lignes"])


def lignes_du_rapport(rapport: dict) -> list[str]:
    """The printed report: connector, key, state — never a value."""
    if rapport["applique"]:
        mode = "APPLIQUÉ" + ("" if rapport["effacer"] else " sans effacer le clair")
    else:
        mode = "À BLANC (rien n'est écrit ; --appliquer)"
    out = [f"secrets-d-application : {mode}"]
    for r in rapport["lignes"]:
        portee = "" if r["scope"] == "platform" else f" [{r['scope']}]"
        out.append(f"  {r['connector']}.{r['key']}{portee} : {r['etat']}")
    if not rapport["lignes"]:
        out.append("  aucun secret dans connector_settings")
    return out
