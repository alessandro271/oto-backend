"""`oto-mcp perimetre export|import` — l'outil en ligne de commande (#1088).

    oto-mcp perimetre export --org 12 [--org 13 …] --sortie perimetre.jsonl
        lit la base de CETTE instance (`DATABASE_URL`), en lecture seule. S'il y a des
        secrets, les rechiffre depuis la clé de cette instance (`OTO_MCP_MASTER_KEY`)
        vers la clé de l'instance cible, lue dans `OTO_EXPORT_CLE_CIBLE` pour cette
        seule exécution — sans elle, un périmètre qui porte des secrets refuse.

    oto-mcp perimetre import perimetre.jsonl
        verse le fichier dans la base de l'instance (`DATABASE_URL`), née par le
        démarrage ; les secrets doivent être chiffrés sous SA clé (`OTO_MCP_MASTER_KEY`).

Un refus s'imprime tel quel, nommé, et sort en code 2 ; rien n'est écrit. Le résumé ne
cite jamais une clé, seulement l'empreinte de la clé cible.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from ..crypto import parse_key
from ..db._conn import _database_url
from .decouverte import ClassementIncomplet
from .extraction import ReferencesHorsPerimetre, SecretsChiffres, exporter
from .importation import ImportRefuse, importer
from .perimetre import PerimetreRefuse
from .rechiffrement import RechiffrementImpossible

REFUS = (ClassementIncomplet, PerimetreRefuse, SecretsChiffres, ReferencesHorsPerimetre,
         RechiffrementImpossible, ImportRefuse, FileExistsError)


def _cle_cible() -> bytes | None:
    brute = os.environ.get("OTO_EXPORT_CLE_CIBLE")
    return parse_key(brute, "OTO_EXPORT_CLE_CIBLE") if brute else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="oto-mcp perimetre",
                                description="Export et import par périmètre de propriétaire.")
    sous = p.add_subparsers(dest="geste", required=True)
    e = sous.add_parser("export", help="exporter le périmètre d'une ou plusieurs orgs")
    e.add_argument("--org", type=int, action="append", required=True, dest="orgs")
    e.add_argument("--sortie", required=True)
    i = sous.add_parser("import", help="importer un export dans la base de cette instance")
    i.add_argument("fichier")
    args = p.parse_args(argv)
    try:
        with psycopg.connect(_database_url(), row_factory=dict_row) as conn:
            if args.geste == "export":
                resultat = exporter(conn, args.orgs, args.sortie, cle_cible=_cle_cible())
                resume = {k: resultat[k] for k in ("perimetre", "tenant", "secrets",
                                                   "cle_cible", "empreinte")}
                resume["lignes"] = {t: v["lignes"] for t, v in resultat["tables"].items()
                                    if v.get("lignes")}
            else:
                resume = {t: v["lignes"] for t, v in importer(conn, args.fichier).items()}
    except REFUS as refus:
        print(f"refus ({type(refus).__name__}) : {refus}", file=sys.stderr)
        return 2
    print(json.dumps(resume, ensure_ascii=False, indent=2))
    return 0
