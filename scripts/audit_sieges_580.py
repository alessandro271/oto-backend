"""Contrôle du passé de #580 : une liaison de messagerie vivante sur la clé PLATEFORME
a-t-elle pris un siège qui n'était pas né de sa demande ?

Avant #580, la réconciliation liait un compte de l'abonnement partagé sur la seule foi
d'un plancher de date, et gardait le compte quand sa date était illisible. Ce script
croise, en LECTURE SEULE, chaque liaison vivante (`unipile_accounts`, `platform_seat`)
avec la date de création du compte chez le fournisseur (`list_accounts`).

**Ce qu'on peut comparer.** La demande de liaison (`unipile_pending`) est SUPPRIMÉE à la
liaison : sa date est perdue. Le repère restant est `connected_at`, posé à la liaison —
quelques minutes, au plus l'heure de vie d'une demande, après elle. Un compte créé chez
le fournisseur plus de `--marge` minutes (défaut 70) AVANT `connected_at` n'a donc pas
été créé par la demande qui l'a lié.

**Ce qui l'explique sans vol, et que le script ne sait pas trancher seul.** Le
fournisseur RÉUTILISE l'identifiant à la reconnexion, et `connected_at` repart à la
reconnexion : une personne qui reconnecte SON compte sort `anterieur`. Une autre ligne du
MÊME sub sur le même identifiant (adoption d'un siège d'une org à l'autre) l'explique ;
une ligne d'un AUTRE sub sur le même identifiant est le signal fort.

Classes, par liaison vivante :
- `coherent` — créé au plus `--marge` minutes avant la liaison ;
- `anterieur` — créé avant : `autres_lignes_meme_sub` et `autres_subs` disent s'il est
  expliqué ou suspect ;
- `date_manquante` — le fournisseur ne donne pas de date lisible ;
- `absent` — l'identifiant n'est plus chez le fournisseur (information).

Sortie : JSON sur la sortie standard — identifiants techniques et dates SEULEMENT
(sub, org, canal, account_id), jamais un nom de compte ni un contenu.

    python -m scripts.audit_sieges_580 [--marge 70]

Sorties : 0 = constat rendu ; 3 = lecture impossible (base ou fournisseur).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from oto_mcp.db import _connect
from oto_mcp.unipile_connect import _parse_dt

_LECTURE = ("SELECT sub, org_id, provider, account_id, platform_seat, connected_at, "
            "disconnected_at FROM unipile_accounts")


def classer(lignes: list[dict], comptes: list[dict], marge: timedelta) -> dict:
    """Le constat, pur : les lignes de la base et l'inventaire du fournisseur."""
    crees = {c.get("id"): c.get("created_at") for c in comptes if c.get("id")}
    par_compte: dict[str, list[dict]] = {}
    for l in lignes:
        par_compte.setdefault(l["account_id"], []).append(l)
    constat: dict[str, list] = {"coherent": [], "anterieur": [], "date_manquante": [],
                                "absent": []}
    for l in lignes:
        if not l["platform_seat"] or l["disconnected_at"] is not None:
            continue
        autres = [o for o in par_compte[l["account_id"]] if o is not l]
        fiche = {"sub": l["sub"], "org_id": l["org_id"], "provider": l["provider"],
                 "account_id": l["account_id"], "lie_le": str(l["connected_at"]),
                 "autres_lignes_meme_sub": sum(1 for o in autres if o["sub"] == l["sub"]),
                 "autres_subs": len({o["sub"] for o in autres if o["sub"] != l["sub"]})}
        if l["account_id"] not in crees:
            constat["absent"].append(fiche)
            continue
        cree = _parse_dt(crees[l["account_id"]])
        lie = _parse_dt(l["connected_at"])
        if cree is None or lie is None:
            constat["date_manquante"].append(fiche)
            continue
        fiche["cree_le"] = cree.isoformat()
        fiche["avance_minutes"] = round((lie - cree).total_seconds() / 60)
        constat["anterieur" if cree < lie - marge else "coherent"].append(fiche)
    lies = {l["account_id"] for l in lignes}
    return {"compte": {k: len(v) for k, v in constat.items()},
            "orphelins_chez_le_fournisseur": sum(1 for a in crees if a not in lies),
            "a_examiner": {k: constat[k] for k in ("anterieur", "date_manquante")},
            "absent": constat["absent"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--marge", type=int, default=70,
                   help="minutes tolérées entre la création et la liaison (défaut 70)")
    args = p.parse_args(argv)
    from oto_mcp.capabilities.unipile_seats import _platform_client
    try:
        with _connect() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            lignes = conn.execute(_LECTURE).fetchall()
        client = _platform_client()
        if client is None:
            print("aucune clé plateforme unipile au coffre", file=sys.stderr)
            return 3
        comptes = client.list_accounts()
    except Exception as e:  # noqa: SILENT — le constat n'a pas eu lieu : dit et code 3
        print(f"lecture impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    print(json.dumps(classer(lignes, comptes, timedelta(minutes=args.marge)),
                     indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
