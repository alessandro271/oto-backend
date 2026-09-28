"""L'extraction : les lignes d'un périmètre, vers un fichier, en lecture seule.

Tout se joue dans UNE transaction `REPEATABLE READ READ ONLY` : un seul instantané,
donc un export cohérent même contre une base servie (production et préproduction
partagent la même), et aucune écriture possible — la base le refuserait.

Avant d'écrire la première ligne, trois refus possibles, chacun nommé :
- le classement ne couvre pas exactement le schéma (`ClassementIncomplet`) ;
- le périmètre est ambigu (`ComptesPartages`) ;
- une ligne exportée porte une valeur chiffrée avec NOTRE clé (`SecretsChiffres`), ou
  une clé étrangère qui pointe hors de l'export (`ReferencesHorsPerimetre`).

Les identifiants sont PRÉSERVÉS : une base née par le démarrage n'en a aucun qui
entre en collision (hors les semences que #969 retire), et l'AAD des secrets contient
l'identité du propriétaire — remapper casserait le déchiffrement. Le manifeste relève
le maximum de chaque séquence pour que l'import la recale.

Format (`FORMAT`) : une ligne JSON par ligne de table, `{"t": <table>, "l": <ligne>}`,
dans un ordre où chaque parent précède ses enfants ; puis une dernière ligne
`{"manifeste": …}`. La ligne est le `row_to_json` de PostgreSQL : elle se relit par
`json_populate_record`, types compris.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import tuple_row

from .classement import CLASSEMENT, EXCLUE, EXPORTEES, INSTANCE, Table
from .decouverte import Cle, Schema, lire_schema, verifier_classement
from .perimetre import Perimetre, resoudre
from .regles import vias

FORMAT = "oto-export-perimetre/1"


class SecretsChiffres(RuntimeError):
    """Des lignes du périmètre portent une valeur chiffrée avec notre clé maîtresse."""

    def __init__(self, comptes: dict[str, int]):
        self.comptes = comptes
        detail = ", ".join(f"{t} : {n}" for t, n in sorted(comptes.items()))
        super().__init__("valeurs chiffrées avec la clé maîtresse de CETTE instance — la "
                         "cible ne peut pas les lire, et le rechiffrement n'est pas écrit "
                         f"(#1088) : {detail}")


class ReferencesHorsPerimetre(RuntimeError):
    """Des clés étrangères de lignes exportées pointent vers des lignes qui ne partent pas."""

    def __init__(self, comptes: dict[str, int]):
        self.comptes = comptes
        detail = "; ".join(f"{k} : {n} ligne(s)" for k, n in sorted(comptes.items()))
        super().__init__("l'export ne serait pas fermé — des lignes pointent hors de "
                         f"ce qui part : {detail}")


def compilateur(classement: dict[str, Table]) -> Callable[[str], str]:
    """`pred(table)` : le prédicat SQL « cette ligne est du périmètre », parents composés."""
    @lru_cache(maxsize=None)
    def pred(table: str) -> str:
        return classement[table].regle.predicat(pred)
    return pred


def ordre_d_export(schema: Schema, classement: dict[str, Table]) -> list[str]:
    """Les tables exportées, chaque parent (FK ou `Via`) avant ses enfants."""
    exportees = sorted(t for t, e in classement.items() if e.classe in EXPORTEES)
    deps = {t: ({k.cible for k in schema.cles_de(t) if k.cible in exportees}
                | {v.parent for v in vias(classement[t].regle)}) - {t}
            for t in exportees}
    ordre: list[str] = []
    while deps:
        prets = sorted(t for t, d in deps.items() if not d - set(ordre))
        if not prets:
            raise RuntimeError(f"cycle de dépendances entre tables exportées : {sorted(deps)}")
        ordre.extend(prets)
        for t in prets:
            del deps[t]
    return ordre


def _compter(conn, sql: str, params: dict) -> int:
    return conn.execute(sql, params).fetchone()["n"]


def controler_secrets(conn, classement, pred, params) -> None:
    comptes = {}
    for t, e in sorted(classement.items()):
        if e.classe in EXPORTEES and e.secrets:
            cond = " OR ".join(f"{c} IS NOT NULL" for c in e.secrets)
            n = _compter(conn, f"SELECT count(*) AS n FROM {t} WHERE ({pred(t)}) AND ({cond})",
                         params)
            if n:
                comptes[t] = n
    if comptes:
        raise SecretsChiffres(comptes)


def _jointure(k: Cle) -> str:
    return " AND ".join(f"p.{cp} = c.{c}" for c, cp in zip(k.colonnes, k.colonnes_cible))


def controler_fermeture(conn, schema: Schema, classement, pred, params) -> dict[str, list]:
    """Refuse une clé étrangère qui sort de l'export ; rend celles qui visent l'INSTANCE.

    Une cible `instance` (le tenant d'une org, par exemple) ne part pas par définition :
    l'instance cible doit porter ces lignes-là, et le manifeste dit lesquelles."""
    hors: dict[str, int] = {}
    vers_instance: dict[str, list] = {}
    for t in sorted(t for t, e in classement.items() if e.classe in EXPORTEES):
        for k in schema.cles_de(t):
            nom = f"{t}({', '.join(k.colonnes)}) → {k.cible}"
            non_nul = " AND ".join(f"c.{c} IS NOT NULL" for c in k.colonnes)
            cible = classement[k.cible]
            if cible.classe == INSTANCE:
                cols = ", ".join(f"c.{c}" for c in k.colonnes)
                vals = [r["v"] for r in conn.execute(
                    f"SELECT DISTINCT ({cols})::text AS v FROM {t} c "
                    f"WHERE ({pred(t)}) AND {non_nul} ORDER BY 1", params)]
                if vals:
                    vers_instance[nom] = vals
                continue
            # Une cible EXCLUE ne part pas : toute référence non nulle sort de l'export.
            absente = ("" if cible.classe == EXCLUE else
                       f" AND NOT EXISTS (SELECT 1 FROM {k.cible} p "
                       f"WHERE {_jointure(k)} AND ({pred(k.cible)}))")
            n = _compter(conn, f"SELECT count(*) AS n FROM {t} c "
                               f"WHERE ({pred(t)}) AND {non_nul}{absente}", params)
            if n:
                hors[nom] = n
    if hors:
        raise ReferencesHorsPerimetre(hors)
    return vers_instance


def _lignes(conn, schema: Schema, t: str, pred, params):
    generees = schema.generees.get(t, frozenset())
    cols = ", ".join(c for c in schema.colonnes[t] if c not in generees)
    tri = ", ".join(schema.primaires.get(t, ())) or "row_to_json(x)::text"
    sql = (f"SELECT row_to_json(x)::text FROM (SELECT {cols} FROM {t} "
           f"WHERE {pred(t)}) x ORDER BY {tri}")
    with conn.cursor(name=f"export_{t}", row_factory=tuple_row) as cur:
        cur.execute(sql, params)
        for (ligne,) in cur:
            yield ligne


def exporter(conn: psycopg.Connection, orgs: list[int], sortie: Path | str, *,
             classement: dict[str, Table] = CLASSEMENT) -> dict:
    """Exporte le périmètre des `orgs` vers `sortie` et rend le manifeste.

    `conn` : une connexion psycopg à `dict_row`, HORS transaction — l'export pose
    lui-même son instantané en lecture seule, et la laisse en lecture seule — une
    connexion confiée à un export n'a plus à écrire. `sortie` ne doit pas exister : on
    n'écrase jamais un export (il s'écrit à côté puis se renomme)."""
    sortie = Path(sortie)
    if sortie.exists():
        raise FileExistsError(f"{sortie} existe déjà : un export ne s'écrase pas")
    conn.read_only = True
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    with conn.transaction():
        schema = lire_schema(conn)
        verifier_classement(schema, classement)
        perimetre = resoudre(conn, orgs)
        params = perimetre.parametres()
        pred = compilateur(classement)
        controler_secrets(conn, classement, pred, params)
        vers_instance = controler_fermeture(conn, schema, classement, pred, params)
        ordre = ordre_d_export(schema, classement)
        provisoire = sortie.with_name(sortie.name + ".partiel")
        with provisoire.open("x", encoding="utf-8") as f:
            manifeste = _ecrire(conn, f, schema, classement, pred, params, ordre)
            manifeste.update(_entete(conn, perimetre), ordre=ordre,
                             references_instance=vers_instance)
            f.write(json.dumps({"manifeste": manifeste}, ensure_ascii=False) + "\n")
        os.replace(provisoire, sortie)
    return manifeste


def _ecrire(conn, f, schema, classement, pred, params, ordre) -> dict:
    empreinte = hashlib.sha256()
    tables: dict[str, dict] = {}
    for t in ordre:
        n = 0
        for ligne in _lignes(conn, schema, t, pred, params):
            texte = f'{{"t": {json.dumps(t)}, "l": {ligne}}}\n'
            empreinte.update(texte.encode())
            f.write(texte)
            n += 1
        tables[t] = {"classe": classement[t].classe, "lignes": n}
    for t, e in sorted(classement.items()):
        if e.classe == EXCLUE:
            tables[t] = {"classe": EXCLUE, "raison": e.raison, "omises": _compter(
                conn, f"SELECT count(*) AS n FROM {t} WHERE {pred(t)}", params)}
        elif e.classe == INSTANCE:
            tables[t] = {"classe": INSTANCE, "raison": e.raison}
    return {"format": FORMAT, "tables": tables, "empreinte": empreinte.hexdigest(),
            "sequences": _sequences(conn, schema, ordre, pred, params),
            "hors_base": _hors_base(conn, classement, ordre, pred, params)}


def _sequences(conn, schema, ordre, pred, params) -> dict[str, int]:
    out = {}
    for t in ordre:
        for c in schema.sequences.get(t, ()):
            m = conn.execute(f"SELECT max({c}) AS m FROM {t} WHERE {pred(t)}", params).fetchone()
            if m["m"] is not None:
                out[f"{t}.{c}"] = m["m"]
    return out


def _hors_base(conn, classement, ordre, pred, params) -> dict[str, list[str]]:
    out = {}
    for t in ordre:
        for c in classement[t].hors_base:
            vals = [r["v"] for r in conn.execute(
                f"SELECT DISTINCT {c} AS v FROM {t} WHERE ({pred(t)}) AND {c} IS NOT NULL "
                "ORDER BY 1", params)]
            if vals:
                out[f"{t}.{c}"] = vals
    return out


def _entete(conn, p: Perimetre) -> dict:
    version = [r["version_num"] for r in conn.execute("SELECT version_num FROM alembic_version")]
    instant = conn.execute("SELECT transaction_timestamp()::text AS t").fetchone()["t"]
    return {"instantane": instant, "version_schema": version,
            "perimetre": {"orgs_declarees": list(p.orgs_declarees),
                          "orgs_personnelles": [o for o in p.orgs
                                                if o not in p.orgs_declarees],
                          "groupes": len(p.groupes), "comptes": len(p.subs)}}
