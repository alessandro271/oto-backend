"""Sceller les jetons de lien encore rangés en clair — `oto-mcp maintenance jetons-de-lien`.

Le code lit et écrit le lien public d'une page et le lien d'une procédure partagée sous
leur forme chiffrée seule (`db/jetons_de_lien.py`) : `*_enc` pour le réafficher, `*_hash`
pour résoudre un lien entrant. Restent les lignes écrites avant, dont le jeton est dans
la colonne en clair (`docs.public_token`, `process_shares.token`) : ce module les scelle.
Une révision ne le peut pas : sceller exige la clé maître (même raison que
`copies-du-coffre`).

- **À blanc par défaut** : par ligne, sa table, son id et son état — jamais un jeton.
- `appliquer=True` : pour chaque ligne qui porte un jeton en clair, UNE transaction
  bornée (`statement_timeout` 15 s, `lock_timeout` 5 s) qui scelle l'enveloppe (AAD de la
  ligne) et l'empreinte, relit et vérifie que l'enveloppe se rouvre sur le même jeton
  (comparé, jamais affiché), puis vide la colonne en clair. Un échec annule la ligne
  entière : jamais un clair vidé sans son enveloppe.
- `effacer=False` (`--sans-effacer`) : scelle et vérifie, garde le clair.
- Avec `effacer`, retire aussi les deux autres copies en clair d'un jeton : celle que
  la fiche d'un compte né d'un partage notait (`acquired_via.token`, remplacée par
  l'identifiant du partage) et celle que la recopie des pages avait posée dans
  `nodes.props`.
- Idempotente : une ligne déjà scellée sur le même jeton est « déjà scellée » (son clair
  vidé avec `effacer`) ; une ligne scellée sur un AUTRE jeton est un écart — laissée
  telle quelle, signalée, code 1 : trancher lequel vaut est un geste d'exploitant.
"""
from __future__ import annotations

import json
import logging

from .db import jetons_de_lien as jetons

logger = logging.getLogger(__name__)

_BORNES = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")

#: table → (colonne en clair, enveloppe, empreinte, AAD de la ligne).
LIENS = {
    "docs": ("public_token", "public_token_enc", "public_token_hash", jetons.aad_page),
    "process_shares": ("token", "token_enc", "token_hash", jetons.aad_partage),
}

A_SCELLER = "à sceller"
SCELLE = "scellé, clair vidé"
SCELLE_CONSERVE = "scellé, clair conservé"
DEJA = "déjà scellé"
DEJA_VIDE = "déjà scellé, clair vidé"
ECART = "écart : l'enveloppe porte un autre jeton — rien n'est touché"

REFUS = frozenset({ECART})


class RelectureEnEchec(RuntimeError):
    """L'enveloppe scellée ne se rouvre pas sur le jeton dont elle vient."""


class ColonnesAbsentes(RuntimeError):
    """La base n'a pas encore les colonnes chiffrées : le code neuf n'y a pas démarré."""


def _borner(conn) -> None:
    for sql in _BORNES:
        conn.execute(sql)


def _exiger_les_colonnes(conn) -> None:
    for table, (_, enc, empreinte, _aad) in LIENS.items():
        n = conn.execute(
            "SELECT count(*) AS n FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name IN (%s, %s)", (table, enc, empreinte)).fetchone()["n"]
        if n != 2:
            raise ColonnesAbsentes(
                f"{table} n'a pas encore {enc}/{empreinte} : démarrer d'abord le code "
                "qui les pose (le démarrage les ajoute)")


def _a_un_clair(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s AND column_name = %s", (table, LIENS[table][0])
    ).fetchone() is not None


def _traiter(conn, table: str, ligne: dict, appliquer: bool, effacer: bool) -> str:
    clair_col, enc_col, hash_col, aad_de = LIENS[table]
    jeton, aad = ligne[clair_col], aad_de(ligne["id"])
    vider = f"UPDATE {table} SET {clair_col} = NULL WHERE id = %s"
    if ligne[enc_col] is not None:
        if jetons.ouvrir(ligne[enc_col], aad) != jeton:
            return ECART
        if ligne[hash_col] != jetons.empreinte(jeton) and appliquer:
            conn.execute(f"UPDATE {table} SET {hash_col} = %s WHERE id = %s",
                         (jetons.empreinte(jeton), ligne["id"]))
        if appliquer and effacer:
            conn.execute(vider, (ligne["id"],))
            return DEJA_VIDE
        return DEJA
    if not appliquer:
        return A_SCELLER
    conn.execute(f"UPDATE {table} SET {enc_col} = %s, {hash_col} = %s WHERE id = %s",
                 (jetons.sceller(jeton, aad), jetons.empreinte(jeton), ligne["id"]))
    relu = conn.execute(f"SELECT {enc_col} FROM {table} WHERE id = %s",
                        (ligne["id"],)).fetchone()[enc_col]
    if jetons.ouvrir(relu, aad) != jeton:
        raise RelectureEnEchec(f"{table} #{ligne['id']} : l'enveloppe ne se rouvre pas sur "
                               "son jeton — rien n'est écrit pour cette ligne")
    if effacer:
        conn.execute(vider, (ligne["id"],))
        return SCELLE
    return SCELLE_CONSERVE


def _fiches(conn, appliquer: bool) -> int:
    """Les fiches de compte qui notent encore le jeton d'un partage (`acquired_via`) ;
    `appliquer` le remplace par l'identifiant du partage, retrouvé par son empreinte —
    ou le retire seul si aucun partage ne le porte plus."""
    rows = conn.execute(
        "SELECT sub, profile FROM user_account_profile "
        "WHERE profile->'acquired_via'->>'kind' = 'process_share' "
        "AND profile->'acquired_via' ? 'token'").fetchall()
    if not appliquer:
        return len(rows)
    for r in rows:
        via = dict(r["profile"]["acquired_via"])
        jeton = via.pop("token")
        share = conn.execute("SELECT id FROM process_shares WHERE token_hash = %s",
                             (jetons.empreinte(jeton or ""),)).fetchone()
        if share is not None:
            via["share_id"] = int(share["id"])
        conn.execute(
            "UPDATE user_account_profile SET profile = jsonb_set(profile, "
            "'{acquired_via}', %s::jsonb), updated_at = NOW() WHERE sub = %s",
            (json.dumps(via), r["sub"]))
    return len(rows)


def ranger(*, appliquer: bool, effacer: bool = True) -> dict:
    """Le rapport : `{"applique", "effacer", "lignes": [{table, id, etat}], "fiches",
    "noeuds"}`. Une transaction bornée par ligne : ce qui est fait le reste si une ligne
    suivante échoue."""
    from .db import _connect
    from .db import nodes as db_nodes

    with _connect() as conn:
        _borner(conn)
        _exiger_les_colonnes(conn)
        a_voir = []
        for table in LIENS:
            if not _a_un_clair(conn, table):
                continue
            a_voir += [(table, int(r["id"])) for r in conn.execute(
                f"SELECT id FROM {table} WHERE {LIENS[table][0]} IS NOT NULL ORDER BY id")]
    lignes = []
    for table, ident in a_voir:
        clair_col, enc_col, hash_col, _aad = LIENS[table]
        with _connect() as conn:
            _borner(conn)
            row = conn.execute(
                f"SELECT id, {clair_col}, {enc_col}, {hash_col} FROM {table} "
                f"WHERE id = %s AND {clair_col} IS NOT NULL"
                + (" FOR UPDATE" if appliquer else ""), (ident,)).fetchone()
            if row is None:            # vidé entre-temps (geste du produit, autre passe)
                continue
            etat = _traiter(conn, table, dict(row), appliquer, effacer)
        if etat in (SCELLE, SCELLE_CONSERVE, DEJA_VIDE):
            logger.info("jetons-de-lien : %s #%s — %s", table, ident, etat)
        lignes.append({"table": table, "id": ident, "etat": etat})
    copies = appliquer and effacer
    with _connect() as conn:
        _borner(conn)
        fiches = _fiches(conn, copies)
        noeuds = db_nodes.retirer_jetons_des_props(conn, appliquer=copies)
    return {"applique": appliquer, "effacer": effacer, "lignes": lignes,
            "fiches": fiches, "noeuds": noeuds}


def refus(rapport: dict) -> bool:
    return any(r["etat"] in REFUS for r in rapport["lignes"])


def reste(rapport: dict) -> bool:
    """Il reste à faire : un jeton pas encore scellé, ou — avec `effacer` — un clair
    encore là, une fiche ou un nœud qui en porte une copie (à blanc : ils sont comptés)."""
    a_faire = {A_SCELLER}
    if rapport["effacer"]:
        a_faire |= {DEJA}
    if any(r["etat"] in a_faire for r in rapport["lignes"]):
        return True
    return rapport["effacer"] and not rapport["applique"] and bool(
        rapport["fiches"] or rapport["noeuds"])


def lignes_du_rapport(rapport: dict) -> list[str]:
    """Le rapport imprimé : table, id, état — jamais un jeton."""
    if rapport["applique"]:
        mode = "APPLIQUÉ" + ("" if rapport["effacer"] else " sans vider le clair")
    else:
        mode = "À BLANC (rien n'est écrit ; --appliquer)"
    out = [f"jetons-de-lien : {mode}"]
    for r in rapport["lignes"]:
        out.append(f"  {r['table']} #{r['id']} : {r['etat']}")
    if not rapport["lignes"]:
        out.append("  aucun jeton de lien en clair")
    verbe = "retirée" if rapport["applique"] and rapport["effacer"] else "à retirer"
    out.append(f"  copie du jeton dans une fiche de compte ({verbe}) : {rapport['fiches']}")
    out.append(f"  copie du jeton dans un nœud ({verbe}) : {rapport['noeuds']}")
    return out
