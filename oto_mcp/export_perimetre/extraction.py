"""L'extraction : les lignes d'un périmètre, vers un fichier, en lecture seule.

Tout se joue dans UNE transaction `REPEATABLE READ READ ONLY` : un seul instantané,
donc un export cohérent même contre une base servie (production et préproduction
partagent la même), et aucune écriture possible — la base le refuserait.

Avant d'écrire la première ligne, ces refus possibles, chacun nommé :
- le classement ne couvre pas exactement le schéma (`ClassementIncomplet`) ;
- le périmètre est ambigu (`perimetre.PerimetreRefuse` et ses cas) ;
- une ligne du périmètre désigne un compte hors périmètre que la règle des anciens
  comptes ne rattache ni n'omet (`comptes.ComptesHorsRegle`) ;
- une ligne exportée porte une valeur chiffrée et l'appelant n'a pas donné la clé de
  l'instance cible (`SecretsChiffres`), ou une clé étrangère pointe hors de l'export
  (`ReferencesHorsPerimetre`).

Les identifiants sont PRÉSERVÉS : une base née par le démarrage n'en a aucun qui
entre en collision, et l'AAD des secrets contient l'identité du propriétaire. Deux
remappages seulement, faits à l'IMPORT et déclarés ici dans le manifeste : le tenant
devient la ligne 1 de la cible (`tenant`), ses subs y perdent leur préfixe (`comptes`).
Les secrets sont rechiffrés ICI, sous la clé de la cible et l'AAD de la ligne cible
(`rechiffrement`, `transformation`) : le fichier ne porte jamais un secret sous notre
clé, notre clé ne sort pas, et le clair n'existe qu'en mémoire.

Les horodatages s'écrivent en UTC (`TimeZone` posé dans la transaction) : la même
ligne relue sur la cible se sérialise à l'identique, ce qui rend l'empreinte
comparable (`importation`).

Format (`FORMAT`) : une ligne JSON par ligne de table, `{"t": <table>, "l": <ligne>}`,
dans un ordre où chaque parent précède ses enfants ; puis une dernière ligne
`{"manifeste": …}`. La ligne est le `row_to_json` de PostgreSQL : elle se relit par
`json_populate_record`, types compris.

`journal=False` laisse le journal d'appels (`classement.JOURNAL`) hors de l'export : il
voyage à part, par tranches de dates (`journal`), et le manifeste compte ce qui reste
(lignes, bornes, maximum de sa séquence).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import tuple_row

from ..crypto import _load_master_key

from .classement import (CLASSEMENT, EXCLUE, EXPORTEES, INSTANCE, JOURNAL, RAISON_JOURNAL,
                         Table, comptes_de, sans_journal)
from .comptes import garde, rattache, recenser
from .decouverte import Cle, Schema, lire_schema, verifier_classement, verifier_journal
from .perimetre import Perimetre, resoudre
from .rechiffrement import AAD, empreinte_cle, rechiffrer
from .objets import COLONNES_DE_CLES, ObjetsRefuses, Stockage, archiver, cles_dans
from .regles import vias
from .transformation import Transformation

FORMAT = "oto-export-perimetre/2"


class SecretsChiffres(RuntimeError):
    """Des lignes du périmètre portent une valeur chiffrée avec notre clé maîtresse."""

    def __init__(self, comptes: dict[str, int]):
        self.comptes = comptes
        detail = ", ".join(f"{t} : {n}" for t, n in sorted(comptes.items()))
        super().__init__("valeurs chiffrées avec la clé maîtresse de CETTE instance — "
                         "l'export les rechiffre pour la cible : lui donner la clé de "
                         f"l'instance cible (`cle_cible`) : {detail}")


class ReferencesHorsPerimetre(RuntimeError):
    """Des clés étrangères de lignes exportées pointent vers des lignes qui ne partent pas."""

    def __init__(self, comptes: dict[str, int]):
        self.comptes = comptes
        detail = "; ".join(f"{k} : {n} ligne(s)" for k, n in sorted(comptes.items()))
        super().__init__("l'export ne serait pas fermé — des lignes pointent hors de "
                         f"ce qui part : {detail}")


def appartenance(entree: Table, pred: Callable[[str], str]) -> str:
    """La règle de la table et, pour un partage, celle de son destinataire."""
    appartient = entree.regle.predicat(pred)
    if entree.destinataire is None:
        return appartient
    # Un partage ne part que si son destinataire est AUSSI du périmètre.
    return f"({appartient}) AND ({entree.destinataire.predicat(pred)})"


def compilateur(classement: dict[str, Table], schema: Schema, *,
                comptes: bool = True) -> Callable[[str], str]:
    """`pred(table)` : le prédicat SQL « cette ligne est du périmètre », parents composés.
    Une ligne dont un compte n'est pas du périmètre n'y est que rattachée à son jumeau
    (`comptes.garde`) ; `comptes=False` la garde telle quelle — pour compter ce que la
    règle retire, jamais pour exporter."""
    @lru_cache(maxsize=None)
    def pred(table: str) -> str:
        entree = classement[table]
        appartient = appartenance(entree, pred)
        colonnes = comptes_de(entree) if comptes else ()
        if not colonnes:
            return appartient
        return f"({appartient}) AND {garde(schema, table, colonnes, appartient)}"
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


def compter_secrets(conn, lu: "Lecture") -> dict[str, int]:
    comptes = {}
    for t in sorted(lu.ordre):
        e = lu.classement[t]
        if e.secrets:
            cond = " OR ".join(f"{c} IS NOT NULL" for c in e.secrets)
            n = _compter(conn, f"SELECT count(*) AS n FROM {t} WHERE ({lu.pred(t)}) "
                               f"AND ({cond})", lu.params)
            if n:
                comptes[t] = n
    return comptes


def compter_partages_omis(conn, lu: "Lecture") -> dict[str, int]:
    """Les partages du périmètre dont le destinataire n'en est pas : omis (décision du
    28/09/2026 — sur la cible ce destinataire n'existe pas, et la ligne y emporterait
    l'identité d'un tiers), et comptés au manifeste."""
    comptes = {}
    for t in sorted(lu.ordre):
        e = lu.classement[t]
        if e.destinataire is not None:
            n = _compter(conn, f"SELECT count(*) AS n FROM {t} WHERE "
                               f"({e.regle.predicat(lu.pred)}) "
                               f"AND NOT ({e.destinataire.predicat(lu.pred)})", lu.params)
            if n:
                comptes[t] = n
    return comptes


def _jointure(k: Cle, comptes: set[str]) -> str:
    """Une colonne-compte rattachée désigne, à la source, le compte de son jumeau."""
    return " AND ".join(f"p.{cp} = {rattache(f'c.{c}') if c in comptes else f'c.{c}'}"
                        for c, cp in zip(k.colonnes, k.colonnes_cible))


def controler_fermeture(conn, lu: "Lecture") -> dict[str, list]:
    """Refuse une clé étrangère qui sort de l'export ; rend celles qui visent l'INSTANCE.

    Une cible `instance` (le tenant d'une org, par exemple) ne part pas par définition :
    l'instance cible doit porter ces lignes-là, et le manifeste dit lesquelles."""
    schema, classement, pred, params = lu.schema, lu.classement, lu.pred, lu.params
    hors: dict[str, int] = {}
    vers_instance: dict[str, list] = {}
    for t in sorted(lu.ordre):
        comptes = {k.colonne for k in comptes_de(classement[t]) if k.simple}
        for k in schema.cles_de(t):
            if k.cible == "tenants" and k.colonnes_cible == ("id",):
                continue  # remappée EN BLOC vers la ligne 1 de la cible (`importation`)
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
                       f"WHERE {_jointure(k, comptes)} AND ({pred(k.cible)}))")
            n = _compter(conn, f"SELECT count(*) AS n FROM {t} c "
                               f"WHERE ({pred(t)}) AND {non_nul}{absente}", params)
            if n:
                hors[nom] = n
    if hors:
        raise ReferencesHorsPerimetre(hors)
    return vers_instance


def _lignes(conn, schema: Schema, t: str, pred, params):
    cols = ", ".join(_colonnes(schema, t))
    tri = ", ".join(schema.primaires.get(t, ())) or "row_to_json(x)::text"
    sql = (f"SELECT row_to_json(x)::text FROM (SELECT {cols} FROM {t} "
           f"WHERE {pred(t)}) x ORDER BY {tri}")
    with conn.cursor(name=f"export_{t}", row_factory=tuple_row) as cur:
        cur.execute(sql, params)
        for (ligne,) in cur:
            yield ligne


@dataclass(frozen=True)
class Lecture:
    """Tout ce qu'il faut pour lire un périmètre dans l'instantané ouvert. `ordre` dit les
    tables lues ; une tranche du journal (`journal.tranche`) n'en garde que le journal,
    ses prédicats bornés à la fenêtre."""
    schema: Schema
    classement: dict[str, Table]
    perimetre: Perimetre
    pred: Callable[[str], str]
    params: dict
    ordre: list[str]
    brut: Callable[[str], str]     # `pred` sans la règle des anciens comptes (`comptes`)
    appartient: Callable[[str], str]   # règle et destinataire, sans la règle des comptes

    def lignes(self, conn, t: str):
        return _lignes(conn, self.schema, t, self.pred, self.params)


def lire(conn: psycopg.Connection, perimetre: Perimetre,
         classement: dict[str, Table] = CLASSEMENT) -> Lecture:
    """Pose le fuseau (UTC) et compile la lecture du `perimetre` — à appeler DANS
    `conn.transaction()`."""
    conn.execute("SET LOCAL TimeZone = 'UTC'")
    schema = lire_schema(conn)
    classement = verifier_classement(schema, classement)
    pred = compilateur(classement, schema)
    return Lecture(schema, classement, perimetre, pred, perimetre.parametres(),
                   ordre_d_export(schema, classement),
                   compilateur(classement, schema, comptes=False),
                   lambda t: appartenance(classement[t], pred))


def ouvrir(conn: psycopg.Connection, orgs: list[int],
           classement: dict[str, Table] = CLASSEMENT) -> Lecture:
    """Pose l'instantané en lecture seule (UTC) et résout le périmètre — à appeler DANS
    `conn.transaction()`, sur une connexion passée en lecture seule avant."""
    return lire(conn, resoudre(conn, orgs), classement)


def lecture_seule(conn: psycopg.Connection) -> None:
    """Une connexion confiée à une lecture de périmètre n'écrit plus, et lit un seul
    instantané. Elle le reste après l'appel."""
    conn.read_only = True
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def exporter(conn: psycopg.Connection, orgs: list[int], sortie: Path | str, *,
             base_publique: str, cle_cible: bytes | None = None,
             stockage: Stockage | None = None,
             classement: dict[str, Table] = CLASSEMENT, journal: bool = True) -> dict:
    """Exporte le périmètre des `orgs` vers `sortie` et rend le manifeste.

    `conn` : une connexion psycopg à `dict_row`, HORS transaction (`lecture_seule`).
    `sortie` ne doit pas exister : on n'écrase jamais un export (il s'écrit à côté
    puis se renomme). `base_publique` : celle de NOTRE stockage public
    (`media_store.public_base`), qui désigne les objets cités par URL.
    `cle_cible` : la clé maîtresse de l'instance cible, pour cette seule exécution —
    les secrets y sont rechiffrés depuis la clé de CETTE instance, et l'archive des
    objets y est scellée. `stockage` : NOTRE stockage objet, où l'export lit les objets
    du périmètre. Sans eux, un périmètre qui porte des secrets ou des objets refuse.
    `journal=False` : le journal d'appels reste (`JOURNAL`) — il se verse à part, par
    tranches (`journal.exporter_tranche`) ; le manifeste le compte."""
    def ouvrir_export(conn) -> Lecture:
        if journal:
            return ouvrir(conn, orgs, classement)
        schema = lire_schema(conn)
        verifier_journal(schema, verifier_classement(schema, classement))
        return ouvrir(conn, orgs, sans_journal(classement))

    def completer(conn, lu: Lecture, manifeste: dict) -> None:
        if journal:
            manifeste["tables"].update(tables_non_exportees(conn, lu))
            manifeste["journal"] = {"inclus": True}
            return
        laisse, sequences = _journal_laisse(conn, lu)
        manifeste["tables"].update(tables_non_exportees(conn, lu, sauf=tuple(JOURNAL)))
        manifeste["tables"].update({t: {"classe": EXCLUE, "raison": RAISON_JOURNAL,
                                        "omises": v["lignes"]} for t, v in laisse.items()})
        manifeste["sequences"].update(sequences)
        manifeste["journal"] = {"inclus": False, "tables": laisse}

    return exporter_lecture(conn, sortie, ouvrir_export, completer,
                            base_publique=base_publique, cle_cible=cle_cible,
                            stockage=stockage)


def exporter_lecture(conn: psycopg.Connection, sortie: Path | str,
                     ouvrir_export: Callable[[psycopg.Connection], Lecture],
                     completer: Callable[[psycopg.Connection, Lecture, dict], None], *,
                     base_publique: str, cle_cible: bytes | None,
                     stockage: Stockage | None) -> dict:
    """Le déroulé commun d'un export : les tables de la lecture que rend
    `ouvrir_export(conn)` (dans l'instantané en lecture seule), les refus, le fichier,
    l'archive des objets et le manifeste, que `completer` achève. Un export existant ne
    s'écrase pas, un export refusé ne laisse rien derrière lui."""
    sortie = Path(sortie)
    archive = sortie.with_name(sortie.name + ".objets.tar")
    for chemin in (sortie, archive):
        if chemin.exists():
            raise FileExistsError(f"{chemin} existe déjà : un export ne s'écrase pas")
    lecture_seule(conn)
    with conn.transaction():
        lu = ouvrir_export(conn)
        anciens = recenser(conn, lu, lu.appartient)
        secrets = compter_secrets(conn, lu)
        if secrets and cle_cible is None:
            raise SecretsChiffres(secrets)
        recoder = _recodeur(lu, cle_cible) if secrets else None
        vers_instance = controler_fermeture(conn, lu)
        provisoire = sortie.with_name(sortie.name + ".partiel")
        try:
            with provisoire.open("x", encoding="utf-8") as f:
                manifeste, cles = _ecrire(conn, f, lu, recoder, base_publique)
                objets = _archiver(cles, stockage, archive, cle_cible, base_publique)
                manifeste.update(
                    _entete(conn, lu.perimetre), ordre=lu.ordre,
                    references_instance=vers_instance, secrets=secrets, objets=objets,
                    partages_omis=compter_partages_omis(conn, lu),
                    comptes_hors_perimetre=anciens,
                    cle_cible=(empreinte_cle(cle_cible) if secrets or objets["liste"]
                               else None),
                    colonnes={t: _colonnes(lu.schema, t) for t in lu.ordre})
                completer(conn, lu, manifeste)
                f.write(json.dumps({"manifeste": manifeste}, ensure_ascii=False) + "\n")
        except BaseException:
            # Un export refusé en chemin ne laisse rien derrière lui, ni lignes ni archive.
            provisoire.unlink(missing_ok=True)
            archive.unlink(missing_ok=True)
            raise
        os.replace(provisoire, sortie)
    return manifeste


def _archiver(cles: set[str], stockage, archive: Path, cle_cible, base_publique) -> dict:
    """L'archive des objets du périmètre, scellée pour la cible, inscrite au manifeste."""
    if not cles:
        return {"archive": None, "empreinte": None, "base_publique": base_publique,
                "liste": {}}
    if stockage is None or cle_cible is None:
        raise ObjetsRefuses(f"le périmètre désigne {len(cles)} objet(s) : l'export les lit "
                            "dans le stockage source (`stockage`) et les scelle sous la clé "
                            "de l'instance cible (`cle_cible`)")
    liste, empreinte = archiver(cles, stockage, archive, cle_cible)
    return {"archive": archive.name, "empreinte": empreinte, "base_publique": base_publique,
            "liste": liste}


def _colonnes(schema: Schema, t: str) -> list[str]:
    generees = schema.generees.get(t, frozenset())
    return [c for c in schema.colonnes[t] if c not in generees]


def _recodeur(lu: Lecture, cle_cible: bytes) -> Callable[[str, str], str]:
    """`recoder(table, ligne)` : la ligne JSON, son secret rechiffré pour la cible —
    sous l'AAD de la ligne que l'import écrira (`Transformation`, la même fonction)."""
    cle_source = _load_master_key()
    if cle_source is None:
        raise RuntimeError("OTO_MCP_MASTER_KEY absente : l'export ne peut pas lire les "
                           "secrets qu'il doit rechiffrer pour la cible")
    transformation = Transformation.depuis(lu.schema, lu.perimetre.comptes_cible())

    def recoder(t: str, ligne: str) -> str:
        if t not in AAD:
            return ligne
        source = json.loads(ligne)
        if source.get(AAD[t][0]) is None:
            return ligne
        source[AAD[t][0]] = rechiffrer(t, source, transformation.appliquer(t, source),
                                       cle_source, cle_cible)
        return json.dumps(source, ensure_ascii=False)
    return recoder


def _ecrire(conn, f, lu: Lecture, recoder, base_publique: str) -> tuple[dict, set[str]]:
    """Écrit les lignes ; rend le début du manifeste et les clés des objets désignés,
    par une colonne de clé ou par une URL de notre stockage, où qu'elle soit."""
    schema, classement, pred, params, ordre = (lu.schema, lu.classement, lu.pred,
                                               lu.params, lu.ordre)
    empreinte = hashlib.sha256()
    tables: dict[str, dict] = {}
    cles = _cles_d_objet(conn, classement, ordre, pred, params)
    for t in ordre:
        n = 0
        for ligne in lu.lignes(conn, t):
            if recoder is not None:
                ligne = recoder(t, ligne)
            cles |= cles_dans(ligne, base_publique)
            texte = f'{{"t": {json.dumps(t)}, "l": {ligne}}}\n'
            empreinte.update(texte.encode())
            f.write(texte)
            n += 1
        tables[t] = {"classe": classement[t].classe, "lignes": n}
    return {"format": FORMAT, "tables": tables, "empreinte": empreinte.hexdigest(),
            "sequences": _sequences(conn, schema, ordre, pred, params)}, cles


def tables_non_exportees(conn, lu: Lecture, sauf: tuple[str, ...] = ()) -> dict[str, dict]:
    """Au manifeste, les tables qui ne partent pas : celles de l'instance (leur raison),
    les exclues (leur raison et les lignes du périmètre laissées)."""
    tables: dict[str, dict] = {}
    for t, e in sorted(lu.classement.items()):
        if t in sauf:
            continue
        if e.classe == EXCLUE:
            tables[t] = {"classe": EXCLUE, "raison": e.raison, "omises": _compter(
                conn, f"SELECT count(*) AS n FROM {t} WHERE {lu.pred(t)}", lu.params)}
        elif e.classe == INSTANCE:
            tables[t] = {"classe": INSTANCE, "raison": e.raison}
    return tables


def _journal_laisse(conn, lu: Lecture) -> tuple[dict[str, dict], dict[str, int]]:
    """Ce que l'export sans journal laisse, en UNE lecture par table : les lignes du
    périmètre (règle brute : ce que les tranches trancheront), les bornes de leur
    horodatage, et le maximum de chaque séquence — que l'import pose sur la cible, pour
    que ses propres appels n'y prennent jamais l'id d'un appel encore à verser."""
    laisse: dict[str, dict] = {}
    sequences: dict[str, int] = {}
    for t, h in sorted(JOURNAL.items()):
        seqs = lu.schema.sequences.get(t, ())
        maxima = "".join(f", max({c}) AS max_{i}" for i, c in enumerate(seqs))
        r = conn.execute(f"SELECT count(*) AS n, to_json(min({h})) #>> '{{}}' AS premier, "
                         f"to_json(max({h})) #>> '{{}}' AS dernier{maxima} FROM {t} "
                         f"WHERE {lu.pred(t)}", lu.params).fetchone()
        laisse[t] = {"horodatage": h, "lignes": r["n"], "premier": r["premier"],
                     "dernier": r["dernier"]}
        sequences.update({f"{t}.{c}": r[f"max_{i}"] for i, c in enumerate(seqs)
                          if r[f"max_{i}"] is not None})
    return laisse, sequences


def _sequences(conn, schema, ordre, pred, params) -> dict[str, int]:
    out = {}
    for t in ordre:
        for c in schema.sequences.get(t, ()):
            m = conn.execute(f"SELECT max({c}) AS m FROM {t} WHERE {pred(t)}", params).fetchone()
            if m["m"] is not None:
                out[f"{t}.{c}"] = m["m"]
    return out


def _cles_d_objet(conn, classement, ordre, pred, params) -> set[str]:
    """Les valeurs des colonnes de CLÉ d'objet ; les colonnes d'URL, elles, sont lues
    avec tout le reste par `cles_dans`."""
    cles: set[str] = set()
    for t in ordre:
        for c in classement[t].hors_base:
            if f"{t}.{c}" in COLONNES_DE_CLES:
                cles |= {r["v"] for r in conn.execute(
                    f"SELECT DISTINCT {c} AS v FROM {t} WHERE ({pred(t)}) "
                    f"AND {c} IS NOT NULL", params)}
    return cles


def _entete(conn, p: Perimetre) -> dict:
    version = [r["version_num"] for r in conn.execute("SELECT version_num FROM alembic_version")]
    instant = conn.execute("SELECT transaction_timestamp()::text AS t").fetchone()["t"]
    return {"instantane": instant, "version_schema": version,
            "perimetre": {"orgs_declarees": list(p.orgs_declarees),
                          "orgs_personnelles": [o for o in p.orgs
                                                if o not in p.orgs_declarees],
                          "groupes": len(p.groupes), "comptes": len(p.subs)},
            "tenant": {"id": p.id_tenant, "slug": p.tenant_slug,
                       "nom": conn.execute("SELECT name FROM tenants WHERE id = %s",
                                           (p.id_tenant,)).fetchone()["name"],
                       "primaire_source": p.tenant_primaire_source},
            "comptes": p.comptes_cible()}
