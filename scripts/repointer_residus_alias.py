"""Reprend les RÉSIDUS d'une fusion de comptes passée : ce qui porte encore un ancien
identifiant de `sub_aliases` alors que son compte a été fusionné — oto-backend#439.

Deux familles de résidus, et un seul geste pour les deux :

- **ce que la fusion abandonnait** : les clés PERSONNELLES (coffre, entité `user` ou
  `member` = `org:sub`). L'AAD dérive de l'entité, donc la fusion les laissait sous
  l'ancien identifiant — invisibles pour leur propriétaire, que la résolution cherche
  sous son sub canonique. Elle les rechiffre désormais (`rekey_personal_credentials`) ;
- **ce qui a été écrit APRÈS la fusion** sous l'ancien identifiant : le journal REST
  attribuait le sub REVENDIQUÉ par le jeton (non résolu) jusqu'à ce que
  l'authentification publie le porteur canonique, et quelques colonnes n'étaient pas
  dans l'inventaire de la fusion.

Le geste est `db.users.repointer_patrimoine(conn, ancien, canonique)` — LE MÊME que
celui de `migrate_sub`, pas une copie : chaque étape ne touche que les lignes qui
portent encore l'ancien identifiant, d'où l'idempotence (rejoué, il ne trouve plus
rien, hors clés laissées et comptées).

**Population** : chaque `old_sub` de `sub_aliases` SANS ligne `users`, dont la chaîne
d'alias aboutit à un compte vivant (`db.resolve_sub`). Sont ÉCARTÉS, et nommés :
- l'alias dont l'ancien identifiant a une ligne `users` — un compte RECRÉÉ après la
  fusion (#439 point 3) : deux comptes vivants, une décision humaine, pas un
  repointage ;
- la chaîne qui ne se résout pas (`AliasNonResolvable`) ;
- le compte canonique EN PAUSE (même garde que `migrate_sub` : on ne verse pas un
  patrimoine dans un compte neutralisé).

**Constat d'abord** : le nombre de lignes par colonne de l'inventaire qui portent un
ancien identifiant de la population, puis les clés personnelles par entité.

UNE transaction, `statement_timeout` et `lock_timeout` bornés. **Passe à blanc par
défaut** : elle REJOUE toutes les écritures puis les ANNULE — une contrainte qui
casserait en vrai casse déjà ici. `--apply` valide. Aucun secret n'est affiché : des
identifiants de compte, des connecteurs, des comptes.

    python -m scripts.repointer_residus_alias            # constat + passe à blanc
    python -m scripts.repointer_residus_alias --apply

Sorties : 0 = fait (ou passe à blanc) ; 3 = une écriture a échoué (tout est annulé).
"""
from __future__ import annotations

import sys

from oto_mcp import db
from oto_mcp.db import _connect
from oto_mcp.db.sub_aliases import AliasNonResolvable
from oto_mcp.db.users import (_MEMBERSHIP_TABLES, _PK_SUB_TABLES, _SUB_COLUMNS,
                              repointer_patrimoine)

_TIMEOUTS = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")


def _colonnes() -> list[tuple[str, str]]:
    """Toutes les colonnes que `repointer_patrimoine` repointe, sans doublon."""
    vues: dict[tuple[str, str], None] = {}
    for t, c in (list(_SUB_COLUMNS) + [(t, c) for t, c, _ in _PK_SUB_TABLES]
                 + [(t, "sub") for t, _ in _MEMBERSHIP_TABLES]
                 + [("user_account_profile", "sub"), ("orgs", "personal_of")]):
        vues[(t, c)] = None
    return list(vues)


def population(conn) -> tuple[list[tuple[str, str]], list[str]]:
    """`([(ancien, canonique)], [écartés, en clair])`."""
    anciens = [r["old_sub"] for r in conn.execute(
        "SELECT a.old_sub, EXISTS (SELECT 1 FROM users u WHERE u.sub = a.old_sub) "
        "AS vivant FROM sub_aliases a ORDER BY a.old_sub").fetchall()
        if not r["vivant"]]
    recrees = [r["old_sub"] for r in conn.execute(
        "SELECT a.old_sub FROM sub_aliases a JOIN users u ON u.sub = a.old_sub "
        "ORDER BY a.old_sub").fetchall()]
    ecartes = [f"{s} : ancien identifiant RECRÉÉ (ligne users vivante) — à trancher "
               "à la main" for s in recrees]
    paires = []
    for ancien in anciens:
        try:
            canonique = db.resolve_sub(ancien)
        except AliasNonResolvable as refus:
            ecartes.append(f"{ancien} : chaîne non résolvable ({refus.motif})")
            continue
        pause = conn.execute("SELECT suspended_at FROM users WHERE sub = %s",
                             (canonique,)).fetchone()
        if pause and pause.get("suspended_at"):
            ecartes.append(f"{ancien} → {canonique} : compte canonique en pause")
            continue
        paires.append((ancien, canonique))
    return paires, ecartes


def constat(conn, anciens: list[str]) -> list[tuple[str, int]]:
    """`[("table.colonne", n)]` des lignes qui portent encore un ancien identifiant,
    puis les clés personnelles (`coffre.user`, `coffre.member`)."""
    out = []
    for t, c in _colonnes():
        n = conn.execute(f"SELECT count(*) AS n FROM {t} WHERE {c} = ANY(%s)",
                         (anciens,)).fetchone()["n"]
        if n:
            out.append((f"{t}.{c}", n))
    n = conn.execute("SELECT count(*) AS n FROM connector_credentials "
                     "WHERE entity_type = 'user' AND entity_id = ANY(%s)",
                     (anciens,)).fetchone()["n"]
    if n:
        out.append(("coffre.user", n))
    n = conn.execute(
        "SELECT count(*) AS n FROM connector_credentials WHERE entity_type = 'member' "
        "AND entity_id ~ '^[0-9]+:' "
        "AND substr(entity_id, strpos(entity_id, ':') + 1) = ANY(%s)",
        (anciens,)).fetchone()["n"]
    if n:
        out.append(("coffre.member", n))
    return out


def main(apply: bool) -> int:
    with _connect() as conn:
        for s in _TIMEOUTS:
            conn.execute(s)
        paires, ecartes = population(conn)
        print(f"{len(paires)} ancien(s) identifiant(s) à reprendre, "
              f"{len(ecartes)} écarté(s)")
        for e in ecartes:
            print(f"  écarté — {e}")
        avant = constat(conn, [a for a, _ in paires]) if paires else []
        print("\nconstat (lignes sous un ancien identifiant) :")
        for nom, n in avant:
            print(f"  {nom:48s} {n}")
        if not avant:
            print("  rien")
        total = {"rekeyed": 0, "collisions": 0, "illisibles": 0}
        try:
            for ancien, canonique in paires:
                bilan = repointer_patrimoine(conn, ancien, canonique)
                for k in total:
                    total[k] += bilan[k]
                if any(bilan.values()):
                    print(f"  {ancien} → {canonique} : coffre {bilan}")
        except Exception as exc:
            conn.rollback()
            print(f"\nÉCHEC ({type(exc).__name__}: {exc}) — tout est annulé",
                  file=sys.stderr)
            return 3
        apres = constat(conn, [a for a, _ in paires]) if paires else []
        print(f"\nclés personnelles : {total['rekeyed']} rechiffrée(s), "
              f"{total['collisions']} laissée(s) (le compte canonique a la sienne), "
              f"{total['illisibles']} illisible(s)")
        print("reste après repointage (attendu : les seules clés laissées) :")
        for nom, n in apres:
            print(f"  {nom:48s} {n}")
        if not apres:
            print("  rien")
        if apply:
            conn.commit()
            print("\ncommité.")
        else:
            conn.rollback()
            print("\npasse à blanc — écritures rejouées puis ANNULÉES "
                  "(--apply pour valider)")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
