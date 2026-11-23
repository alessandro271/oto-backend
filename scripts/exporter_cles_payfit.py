"""Exporte les clés PayFit d'UNE équipe, en clair, vers la sortie standard d'un tuyau —
pour les verser dans le gestionnaire de secrets d'un autre outil sans jamais les
afficher ni les écrire sur disque.

Ce qu'il lit : les lignes du coffre `(group, <équipe>, payfit, <compte>)`, une par
société, déchiffrées par `credentials_store.get_credential_with_meta` (jamais à la
main : le chiffré est lié par AAD aux quatre colonnes de sa ligne), puis relues par
`credentials_store.unpack_secret` (PayFit : un seul champ, la valeur brute de la clé).

Ce qu'il écrit sur stdout, et RIEN d'autre : une seule ligne JSON, sans saut de ligne
final, `[{"societe":"…","cle":"…"}, …]` — aucun autre champ.

- `societe` = `meta.label` de la ligne, sinon son `account`, normalisé : minuscules,
  accents ôtés, tout ce qui n'est pas `[a-z0-9]` remplacé par un tiret, tirets répétés
  réduits, tirets de bord ôtés ; le résultat doit vérifier `^[a-z0-9][a-z0-9-]*$`.
- `cle` = la clé telle que stockée.

Sur stderr : des comptes, et pour chaque ligne son `account` en face de son nom court
(de quoi retrouver la ligne) — jamais une valeur de clé.

**Tout ou rien.** Le JSON n'est écrit qu'une fois TOUTES les lignes lues et validées,
en un seul `write`. Toute anomalie lève, et rien ne sort sur stdout :
- l'équipe n'existe pas, ou n'appartient pas à l'org passée en argument ;
- aucune ligne PayFit dans l'équipe ;
- un nom court vide ou hors du motif, ou deux lignes qui donnent le même nom court
  (aucun suffixe n'est inventé) ;
- une clé de moins de `LONGUEUR_MIN` caractères ;
- une ligne qui ne se déchiffre pas ou ne se relit pas.

**Une ligne suspendue (`meta.suspended`) est EXCLUE**, et signalée sur stderr avec son
compte : la résolution la saute déjà (`credentials_store.instance_suspended`), la
verser ailleurs rendrait vivante une clé qu'on a voulu éteindre. Si toutes les lignes
sont suspendues, rien ne sort (erreur).

**Refus si stdout est un terminal** : la sortie ne s'écrit que dans un tuyau.

**Aucune valeur dans une trace.** Une exception levée pendant la lecture est réduite
à son type ; sa chaîne (qui pourrait recopier un fragment déchiffré) ne s'affiche pas,
et `main` n'imprime jamais de pile.

`--compter` ne sort RIEN sur stdout et accepte un terminal : il fait toutes les
lectures et toutes les vérifications, déchiffrement compris, puis n'écrit sur stderr
que le nombre de lignes, les exclues, et chaque `account` en face de son nom court.
C'est lui qu'on lance d'abord.

Le connecteur est figé (`payfit`) : le script ne sert à rien d'autre. Lecture seule.

    deploy/lanceur_secrets.py --script scripts/exporter_cles_payfit.py \\
        --org <id> --equipe <id> --compter
    deploy/lanceur_secrets.py --script scripts/exporter_cles_payfit.py \\
        --org <id> --equipe <id> | <commande qui lit stdin>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Optional, TextIO

CONNECTEUR = "payfit"
LONGUEUR_MIN = 20
MOTIF_NOM = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class Refus(Exception):
    """Une anomalie : rien ne sort sur stdout. Le message ne porte jamais de valeur."""


@dataclass(frozen=True)
class Ligne:
    account: str
    meta: dict


def nom_court(brut: str) -> str:
    """`meta.label` ou `account` → nom court `^[a-z0-9][a-z0-9-]*$`, ou `Refus`."""
    decompose = unicodedata.normalize("NFKD", brut or "")
    sans_accent = "".join(c for c in decompose if not unicodedata.combining(c))
    nom = re.sub(r"[^a-z0-9]+", "-", sans_accent.lower()).strip("-")
    if not MOTIF_NOM.match(nom):
        raise Refus(f"nom court vide ou invalide pour le libellé {brut!r}")
    return nom


# --- Lectures (une fonction chacune : les tests les remplacent) ----------------------

def lire_equipe(equipe: int) -> Optional[dict]:
    from oto_mcp import group_store
    return group_store.get_group(equipe)


def lire_lignes(equipe: int) -> list[Ligne]:
    """Les lignes PayFit de l'équipe, SANS leur secret (compte et meta)."""
    from oto_mcp.db import _connect
    with _connect() as conn:
        rows = conn.execute(
            "SELECT account, meta FROM connector_credentials "
            "WHERE entity_type = 'group' AND entity_id = %s AND connector = %s "
            "ORDER BY account",
            (str(equipe), CONNECTEUR),
        ).fetchall()
    return [Ligne(account=r["account"], meta=r["meta"] or {}) for r in rows]


def lire_cle(equipe: int, account: str) -> str:
    """La clé en clair d'une ligne, par le coffre. Toute erreur est réduite à son type."""
    from oto_mcp import credentials_store as cs
    try:
        lue = cs.get_credential_with_meta("group", str(equipe), CONNECTEUR, account)
        if lue is None:
            raise Refus(f"compte {account!r} : la ligne a disparu pendant la lecture")
        champs = cs.unpack_secret(CONNECTEUR, lue["secret"])
    except Refus:
        raise
    except Exception as e:  # noqa: BLE001 — réduite à son type : jamais sa chaîne
        raise Refus(f"compte {account!r} : clé illisible ({type(e).__name__})") from None
    if len(champs) != 1:
        raise Refus(f"compte {account!r} : {len(champs)} champs, un seul attendu")
    (cle,) = champs.values()
    if not isinstance(cle, str):
        raise Refus(f"compte {account!r} : la clé n'est pas une chaîne")
    return cle


# --- Le cœur ----------------------------------------------------------------------

def collecter(org: int, equipe: int, err: TextIO) -> list[dict]:
    """Lit et valide TOUT ; rend la liste à écrire, ou lève `Refus` sans rien rendre."""
    groupe = lire_equipe(equipe)
    if groupe is None:
        raise Refus(f"l'équipe {equipe} n'existe pas")
    if int(groupe["org_id"]) != org:
        raise Refus(f"l'équipe {equipe} n'appartient pas à l'org {org}")

    lignes = lire_lignes(equipe)
    if not lignes:
        raise Refus(f"aucune clé {CONNECTEUR} dans l'équipe {equipe}")

    sortie: list[dict] = []
    vus: dict[str, str] = {}
    exclues = 0
    for ligne in lignes:
        nom = nom_court(ligne.meta.get("label") or ligne.account)
        if ligne.meta.get("suspended"):
            exclues += 1
            print(f"exclue (suspendue) : compte {ligne.account!r} → {nom}", file=err)
            continue
        if nom in vus:
            raise Refus(f"nom court {nom!r} en double : comptes {vus[nom]!r} et "
                        f"{ligne.account!r}")
        vus[nom] = ligne.account
        cle = lire_cle(equipe, ligne.account)
        if len(cle) < LONGUEUR_MIN:
            raise Refus(f"compte {ligne.account!r} : clé de {len(cle)} caractères, "
                        f"{LONGUEUR_MIN} au moins")
        print(f"compte {ligne.account!r} → {nom}", file=err)
        sortie.append({"societe": nom, "cle": cle})

    if not sortie:
        raise Refus(f"toutes les lignes sont suspendues ({exclues})")
    print(f"{len(lignes)} ligne(s) lue(s), {len(sortie)} retenue(s), "
          f"{exclues} exclue(s)", file=err)
    return sortie


def executer(org: int, equipe: int, compter: bool,
             out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    if not compter and out.isatty():
        print("REFUS — la sortie est un terminal : elle ne s'écrit que dans un tuyau "
              "(ou --compter)", file=err)
        return 2
    try:
        sortie = collecter(org, equipe, err)
    except Refus as refus:
        print(f"REFUS — {refus} ; rien n'est sorti", file=err)
        return 1
    if compter:
        print("--compter : aucune clé écrite", file=err)
        return 0
    out.write(json.dumps(sortie, ensure_ascii=False, separators=(",", ":")))
    out.flush()
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--org", type=int, required=True, help="org qui possède l'équipe")
    p.add_argument("--equipe", type=int, required=True, help="équipe qui porte les clés")
    p.add_argument("--compter", action="store_true",
                   help="lit et vérifie tout, n'écrit que des comptes sur stderr")
    a = p.parse_args(argv)
    try:
        return executer(a.org, a.equipe, a.compter)
    except Exception as e:  # noqa: SILENT — jamais de pile : elle pourrait porter une valeur ; le type seul sort
        print(f"ERREUR — {type(e).__name__} ; rien n'est sorti", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
