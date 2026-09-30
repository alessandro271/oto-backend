#!/usr/bin/env python3
"""Résorbe les sélections de connecteur restées sous l'ancienne sentinelle `org_id=0`
(oto-backend#959, suite de #868).

⚠️ **À BLANC PAR DÉFAUT.** `--appliquer` écrit ; sans lui, rien ne bouge et le script
rend ce qu'il ferait, chiffré. Même règle que `tableaux_par_numero.py`.

## Pourquoi ce script existe

`org_id=0` était l'« espace perso sans org » (ADR 0015), retiré par ADR 0030 §8. Toute
lecture de la sélection se fait sous l'org RÉELLE du membre : une ligne sous `0` n'est
rendue par rien (`connectors.me` rend 0 connecteur pour un compte qui en porte 60 sous
`0`). Ces lignes ne sont PAS, pour l'essentiel, des choix de personnes :

    10/07   le backfill ADR 0050 a semé « ce que chacun voyait » sous la paire (sub, 0)
    28/08   le fan-out du split unipile les a recopiées sous `0`
    27/09   le fan-out du split google aussi

Les repointer vers l'org réelle INSTALLERAIT donc chez chacun le catalogue d'alors,
par-dessus la sélection qu'il s'est faite depuis. D'où des actions séparées, chiffrées
à blanc, et un choix EXPLICITE pour celle qui décide (`--restes`).

    disparus   ligne vers un connecteur qui n'est plus au registre       → supprimée
    doublons   connecteur déjà sélectionné, ou RETIRÉ par le membre, sous son
               org réelle : l'org réelle fait foi (son geste est le plus récent)
                                                                         → supprimée
    restes     connecteur absent de l'org réelle : `--restes purger` (la ligne
               part) ou `--restes repointer` (elle passe sous l'org réelle, son
               état gardé). Obligatoire dès qu'il y en a, sous `--appliquer`.
    orphelins  compte SANS org active (souvent un compte qui n'existe plus) :
               listés, supprimés seulement avec `--purger-orphelins`

Les marques de `connector_selection_seeded` sous `0` ne sont PAS touchées : trois
d'entre elles sont les sentinelles des migrations de boot (`#adr0050-backfill`,
`#…-split-fanout`), et une marque sans ligne ne sert rien.

L'état d'AVANT est écrit dans `--sauvegarde` dans le MÊME geste, avant la première
écriture : c'est lui qui permet de revenir en arrière. Chaque écriture est conditionnée
sur ce qui a été lu : ce qui a bougé entre les deux passes n'est pas écrasé.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from collections import Counter, defaultdict

SECTIONS = ("disparus", "doublons", "restes", "orphelins")
RESTES = ("purger", "repointer")


def classer(cur) -> list[dict]:
    """Chaque ligne sous `0`, avec son sort. Une seule lecture, aucune écriture."""
    from oto_mcp import providers

    cur.execute(
        "SELECT u.sub, u.connector, u.state, u.origin, u.selected_at, "
        "       (SELECT m.org_id FROM org_members m "
        "         WHERE m.sub = u.sub AND m.is_active LIMIT 1) AS maison "
        "  FROM user_selected_connectors u WHERE u.org_id = 0 "
        " ORDER BY u.sub, u.connector")
    lignes = [dict(r) for r in cur.fetchall()]
    maisons = {l["maison"] for l in lignes if l["maison"] is not None}
    tenues: set = set()
    if maisons:
        cur.execute(
            "SELECT sub, org_id, connector FROM user_selected_connectors "
            " WHERE org_id = ANY(%s) "
            "UNION SELECT sub, org_id, connector FROM connector_selection_removed "
            " WHERE org_id = ANY(%s)", (list(maisons), list(maisons)))
        tenues = {(r["sub"], r["org_id"], r["connector"]) for r in cur.fetchall()}
    for l in lignes:
        if l["connector"] not in providers.REGISTRY:
            l["sort"] = "disparus"
        elif l["maison"] is None:
            l["sort"] = "orphelins"
        elif (l["sub"], l["maison"], l["connector"]) in tenues:
            l["sort"] = "doublons"
        else:
            l["sort"] = "restes"
    return lignes


def _supprimer(cur, l: dict) -> int:
    cur.execute("DELETE FROM user_selected_connectors "
                " WHERE sub = %s AND org_id = 0 AND connector = %s AND state = %s",
                (l["sub"], l["connector"], l["state"]))
    return cur.rowcount or 0


def _repointer(cur, l: dict) -> int:
    # Conditionné : la ligne n'a pas bougé, et rien n'est apparu sous l'org réelle
    # entre les deux passes (sinon la PK casserait — et ce serait un doublon).
    cur.execute(
        "UPDATE user_selected_connectors a SET org_id = %s "
        " WHERE a.sub = %s AND a.org_id = 0 AND a.connector = %s AND a.state = %s "
        "   AND NOT EXISTS (SELECT 1 FROM user_selected_connectors b "
        "                    WHERE b.sub = a.sub AND b.org_id = %s "
        "                      AND b.connector = a.connector) "
        "   AND NOT EXISTS (SELECT 1 FROM connector_selection_removed r "
        "                    WHERE r.sub = a.sub AND r.org_id = %s "
        "                      AND r.connector = a.connector)",
        (l["maison"], l["sub"], l["connector"], l["state"], l["maison"], l["maison"]))
    return cur.rowcount or 0


def agir(lignes: list[dict], sections, restes, purger_orphelins: bool) -> list[dict]:
    """Ce qui sera ÉCRIT : chaque ligne reçoit son `geste` (ou aucun)."""
    gestes = []
    for l in lignes:
        if l["sort"] not in sections:
            continue
        if l["sort"] in ("disparus", "doublons"):
            geste = "supprimer"
        elif l["sort"] == "orphelins":
            geste = "supprimer" if purger_orphelins else None
        else:
            geste = {"purger": "supprimer", "repointer": "repointer"}.get(restes)
        if geste:
            gestes.append({**l, "geste": geste})
    return gestes


def rapport(lignes: list[dict]) -> dict:
    par_sort = Counter(l["sort"] for l in lignes)
    comptes = defaultdict(set)
    for l in lignes:
        comptes[l["sort"]].add(l["sub"])
    return {"lignes": par_sort, "comptes": {k: len(v) for k, v in comptes.items()},
            "total": len(lignes), "total_comptes": len({l["sub"] for l in lignes})}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--appliquer", action="store_true",
                    help="ÉCRIT en base. Sans lui : à blanc, rien n'est modifié.")
    ap.add_argument("--seulement", choices=SECTIONS, action="append",
                    help="Une section (répétable). Défaut : les quatre.")
    ap.add_argument("--restes", choices=RESTES,
                    help="Le sort des lignes absentes de l'org réelle. Obligatoire sous "
                         "--appliquer dès qu'il y en a.")
    ap.add_argument("--purger-orphelins", action="store_true",
                    help="Supprime aussi les lignes des comptes SANS org active.")
    ap.add_argument("--sauvegarde", default="/opt/oto-mcp/sauvegardes",
                    help="Où écrire l'état AVANT, dans le même geste que l'écriture.")
    ap.add_argument("--details", action="store_true",
                    help="Détail par compte (identifiants et comptes seulement).")
    args = ap.parse_args(argv[1:])
    sections = args.seulement or list(SECTIONS)

    from oto_mcp.db._conn import _connect

    with _connect() as conn, conn.cursor() as cur:
        lignes = classer(cur)
    r = rapport(lignes)
    if (args.appliquer and "restes" in sections and r["lignes"].get("restes")
            and not args.restes):
        print("  ⛔ des lignes n'ont pas d'équivalent sous l'org réelle : choisis "
              "--restes purger|repointer. Rien n'est écrit.")
        return 2
    gestes = agir(lignes, sections, args.restes, args.purger_orphelins)

    fait = Counter()
    if args.appliquer and gestes:
        rep = pathlib.Path(args.sauvegarde)
        rep.mkdir(parents=True, exist_ok=True)
        quand = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        f = rep / f"selections-org-zero-{quand}.json"
        f.write_text(json.dumps(gestes, ensure_ascii=False, default=str))
        if not f.exists() or f.stat().st_size == 0:
            print("  ⛔ sauvegarde ILLISIBLE — rien n'est écrit en base.")
            return 2
        print(f"  ⤷ sauvegarde AVANT : {f} ({len(gestes)} lignes)")
        with _connect() as conn, conn.cursor() as cur:
            for g in gestes:
                n = (_repointer if g["geste"] == "repointer" else _supprimer)(cur, g)
                fait[(g["sort"], g["geste"] if n else "inchangée (a bougé)")] += 1
            conn.commit()

    mode = "APPLIQUÉ" if args.appliquer else "À BLANC — rien n'a été écrit"
    print(f"=== {mode} ===")
    print(f"  lignes sous org_id=0 : {r['total']} ({r['total_comptes']} comptes)")
    for s in SECTIONS:
        print(f"  {s:<10} {r['lignes'].get(s, 0):>6} lignes  "
              f"{r['comptes'].get(s, 0):>4} comptes")
    if not args.appliquer:
        print("\n  Ce que ferait --appliquer :")
        for g, n in sorted(Counter((x["sort"], x["geste"]) for x in agir(
                lignes, sections, args.restes or "purger", args.purger_orphelins)).items()):
            print(f"    {g[0]:<10} {g[1]:<10} {n}")
        if r["lignes"].get("restes") and not args.restes:
            print("    (restes comptés avec --restes purger ; "
                  f"avec repointer : {r['lignes']['restes']} lignes passeraient sous "
                  "l'org réelle)")
    else:
        for (s, g), n in sorted(fait.items()):
            print(f"    {s:<10} {g:<22} {n}")
    if args.details:
        par_compte = defaultdict(Counter)
        maison = {}
        for l in lignes:
            par_compte[l["sub"]][l["sort"]] += 1
            maison[l["sub"]] = l["maison"]
        print("\n[par compte]")
        for sub, c in sorted(par_compte.items()):
            print(f"  {sub:<28} org {maison[sub]!s:<6} "
                  + " ".join(f"{k}={c[k]}" for k in SECTIONS if c[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
