#!/usr/bin/env python3
"""Le DDL assemblé, FIGÉ par fragment : un fichier par fragment, plus un fichier d'ordre.

oto-backend#789 (absorbe oto#87). Le gel tenait une empreinte UNIQUE de la chaîne
entière, commentaires compris : deux lots sur deux domaines différents entraient en
conflit sur la même ligne, et retirer un commentaire SQL la cassait alors que rien
d'exécutable n'avait bougé. Le gel garde sa raison d'être — un changement du DDL servi
est un ACTE DÉLIBÉRÉ, visible en revue — sous une forme qui ne fait plus se toucher deux
lots indépendants :

- `tests/schema_gele/<module>.<CONSTANTE>.sql` : le DDL d'UN fragment de
  `_schema.ASSEMBLAGE`, sous sa forme NORMALISÉE (`normaliser`) ;
- `tests/schema_gele/ORDRE` : la liste ordonnée des fragments, une ligne chacun. L'ordre
  est une contrainte d'exécution (une FK vers une table pas encore créée échoue sur une
  base vierge) qui ne se décompose pas par fragment : il est figé à part.

Normalisation (déterministe) : les commentaires SQL (`-- …` jusqu'à la fin de la ligne,
et `/* … */`) sont retirés hors des littéraux `'…'` et des identifiants `"…"` ; hors de
ces mêmes littéraux, un blanc qui contient un saut de ligne devient un saut de ligne et
tout autre blanc une espace ; chaque ligne est rognée et les lignes vides tombent. Un
commentaire réécrit, une ré-indentation ou une ligne vide ne changent donc rien ; tout
ordre SQL changé, ajouté ou retiré, si.

Usage, à la racine du dépôt :

    python -m scripts.schema_gele           # compare ; sort en 1 avec le diff s'il diverge
    python -m scripts.schema_gele --regen   # RÉÉCRIT les fichiers figés — en local seulement

⚠️ `--regen` n'est jamais joué en CI : la CI compare, elle ne réécrit pas. Régénérer est
le geste délibéré, et le diff des fichiers figés doit partir DANS le commit qui change le
DDL, pour être lu en revue (`tests/test_schema_assembly_frozen.py`).
"""
from __future__ import annotations

import difflib
import pathlib
import sys

from oto_mcp.db import _schema, schema

RACINE = pathlib.Path(__file__).resolve().parent.parent
DOSSIER = RACINE / "tests" / "schema_gele"
ORDRE = DOSSIER / "ORDRE"
SUFFIXE = ".sql"
COMMANDE = "python -m scripts.schema_gele --regen"


def _sans_commentaires(sql: str) -> str:
    """Retire `-- …` et `/* … */` hors des littéraux `'…'` et des identifiants `"…"`."""
    sortie: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # '' ou "" : guillemet échappé
                        j += 2
                        continue
                    break
                j += 1
            sortie.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            fin = sql.find("\n", i)
            i = n if fin < 0 else fin
        elif sql.startswith("/*", i):
            fin = sql.find("*/", i + 2)
            i = n if fin < 0 else fin + 2
            sortie.append(" ")
        else:
            sortie.append(c)
            i += 1
    return "".join(sortie)


def _blancs(sql: str) -> str:
    """Hors des littéraux : un blanc avec saut de ligne → `\\n`, sinon → une espace."""
    sortie: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            sortie.append(sql[i:j + 1])
            i = j + 1
        elif c.isspace():
            j = i
            while j < n and sql[j].isspace():
                j += 1
            sortie.append("\n" if "\n" in sql[i:j] else " ")
            i = j
        else:
            sortie.append(c)
            i += 1
    return "".join(sortie)


def normaliser(sql: str) -> str:
    """La forme figée d'un fragment : sans commentaires, blancs normalisés."""
    lignes = (ligne.strip() for ligne in _blancs(_sans_commentaires(sql)).split("\n"))
    return "\n".join(ligne for ligne in lignes if ligne) + "\n"


def fragments() -> list[tuple[str, str]]:
    """`(module.CONSTANTE, DDL)` dans l'ordre de `_schema.ASSEMBLAGE`.

    Le nom se retrouve par IDENTITÉ de la chaîne (le motif du test des orphelins) : un
    fragment assemblé qu'aucune constante publique d'un module de `schema` ne porte
    n'a pas de nom, et c'est une erreur."""
    noms: dict[int, str] = {}
    for nom_module in schema.__all__:
        module = getattr(schema, nom_module)
        for const in dir(module):
            valeur = getattr(module, const)
            if not const.startswith("_") and isinstance(valeur, str):
                noms.setdefault(id(valeur), f"{nom_module}.{const}")
    sortie = []
    for fragment in _schema.ASSEMBLAGE:
        nom = noms.get(id(fragment))
        if nom is None:
            raise LookupError(
                "un fragment de `_schema.ASSEMBLAGE` n'est porté par aucune constante "
                f"publique de `oto_mcp.db.schema` : {fragment[:80]!r}…")
        sortie.append((nom, fragment))
    return sortie


def fichier(nom: str) -> pathlib.Path:
    return DOSSIER / f"{nom}{SUFFIXE}"


def ordre_fige() -> list[str]:
    return [ligne for ligne in ORDRE.read_text(encoding="utf-8").splitlines() if ligne]


def ecarts() -> list[str]:
    """Chaque divergence entre le DDL servi et le DDL figé, avec son diff lisible."""
    servis = fragments()
    noms = [nom for nom, _ in servis]
    sortie: list[str] = []

    fige = ordre_fige() if ORDRE.exists() else []
    if fige != noms:
        sortie.append("ORDRE d'assemblage changé :\n" + "".join(difflib.unified_diff(
            [f"{n}\n" for n in fige], [f"{n}\n" for n in noms],
            "tests/schema_gele/ORDRE (figé)", "_schema.ASSEMBLAGE (servi)")))

    for nom, ddl in servis:
        chemin = fichier(nom)
        attendu = chemin.read_text(encoding="utf-8") if chemin.exists() else ""
        servi = normaliser(ddl)
        if servi != attendu:
            sortie.append(f"fragment {nom} :\n" + "".join(difflib.unified_diff(
                attendu.splitlines(keepends=True), servi.splitlines(keepends=True),
                f"{chemin.relative_to(RACINE)} (figé)", f"{nom} (servi)")))

    attendus = {fichier(nom).name for nom in noms}
    for perime in sorted(p.name for p in DOSSIER.glob(f"*{SUFFIXE}")):
        if perime not in attendus:
            sortie.append(f"fichier figé sans fragment assemblé : tests/schema_gele/{perime}")
    return sortie


def regenerer() -> list[pathlib.Path]:
    """Réécrit les fichiers figés et l'ordre depuis le DDL servi. En LOCAL seulement."""
    DOSSIER.mkdir(parents=True, exist_ok=True)
    servis = fragments()
    ecrits = []
    for nom, ddl in servis:
        chemin = fichier(nom)
        chemin.write_text(normaliser(ddl), encoding="utf-8")
        ecrits.append(chemin)
    ORDRE.write_text("".join(f"{nom}\n" for nom, _ in servis), encoding="utf-8")
    ecrits.append(ORDRE)
    attendus = {fichier(nom).name for nom, _ in servis}
    for perime in DOSSIER.glob(f"*{SUFFIXE}"):
        if perime.name not in attendus:
            perime.unlink()
    return ecrits


def main(argv: list[str]) -> int:
    if "--regen" in argv:
        regenerer()
        print(f"DDL figé réécrit dans {DOSSIER.relative_to(RACINE)} — relis le diff et "
              "commite-le avec le changement de DDL.")
        return 0
    trouves = ecarts()
    if trouves:
        print("\n\n".join(trouves))
        print(f"\nSi c'est délibéré : `{COMMANDE}`, puis commite le diff avec le DDL.")
        return 1
    print("DDL servi = DDL figé.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
