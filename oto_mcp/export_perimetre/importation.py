"""L'import d'un export de périmètre dans une base NÉE PAR LE DÉMARRAGE (#969).

La cible est une base vierge qu'`init_db` a montée pour l'instance qui la sert : même
schéma, version Alembic posée, tenant primaire (ligne 1) semé depuis
`OTO_TENANT_PRIMAIRE_SLUG`. L'import y verse le fichier en UNE transaction, et ne la
valide qu'après s'être relu.

Refus, tous AVANT la première écriture et chacun nommé (`ImportRefuse`) : fichier dont
l'empreinte ou les comptes ne sont pas ceux du manifeste, schéma ou version
différents de la source, base cible déjà peuplée, tenant primaire de la cible dont
le slug ou le NOM (semé depuis `OTO_BRAND_NAME`) n'est pas celui du tenant exporté,
secrets ou objets chiffrés sous une autre clé que celle de CETTE instance, archive des
objets absente ou modifiée.

L'import ne connaît que la clé de son instance : les secrets arrivent déjà rechiffrés
pour elle (`rechiffrement`, fait à l'export). Il vérifie seulement, en mémoire, que
chacun se déchiffre sous sa clé et l'AAD de la ligne qu'il écrit.

Ce qui change en chemin est `transformation.Transformation`, la fonction même dont
l'export s'est servi pour les AAD : le tenant devient la ligne 1 (la ligne semée prend
ses valeurs), toute clé vers `tenants(id)` vaut 1, les comptes perdent leur préfixe, et
les URL de notre stockage public deviennent celles du stockage de la cible.

Les objets (`objets`) se versent de l'archive dans le stockage de la cible, après la
relecture et avant la validation : un objet qui ne se verse pas annule tout, et un
nouvel essai saute ceux qui sont déjà là. La relecture refuse s'il subsiste la moindre
URL de notre stockage dans le périmètre.

L'écriture se fait par LOTS (`TAILLE_LOT` lignes par aller-retour) : un journal
d'appels complet se compte en centaines de milliers de lignes.

La vérification finale relit le périmètre SUR LA CIBLE, par la même lecture que
l'export (`extraction.ouvrir`), et exige par table le même nombre de lignes que le
manifeste et la même empreinte que les lignes écrites. L'empreinte est une somme de
hachés (indépendante de l'ordre) de la forme canonique de chaque ligne
(`json.dumps(sort_keys=True)`) — la seule comparaison qui survive aux remappages.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import psycopg

from ..crypto import _load_master_key
from .classement import CLASSEMENT
from .decouverte import lire_schema, verifier_classement
from .extraction import FORMAT, _colonnes, ouvrir
from .objets import ObjetsRefuses, Stockage, controler_archive
from .objets import verser as verser_objets
from .rechiffrement import AAD, empreinte_cle, lisible
from .transformation import Transformation

_MODULE = 2 ** 256
TAILLE_LOT = 500


class ImportRefuse(RuntimeError):
    """L'import ne part pas : la cible ou le fichier ne sont pas ce qu'ils doivent être."""


class VerificationEchouee(ImportRefuse):
    """Relue, la cible ne porte pas ce que l'import y a écrit : tout est annulé."""


def lire_manifeste(chemin: Path) -> dict:
    derniere = None
    with chemin.open(encoding="utf-8") as f:
        for derniere in f:
            pass
    if derniere is None:
        raise ImportRefuse(f"{chemin} est vide")
    manifeste = json.loads(derniere).get("manifeste")
    if not manifeste or manifeste.get("format") != FORMAT:
        raise ImportRefuse(f"{chemin} n'est pas un export au format {FORMAT}")
    return manifeste


def _lignes_du_fichier(chemin: Path):
    with chemin.open(encoding="utf-8") as f:
        for texte in f:
            if texte.startswith('{"manifeste"'):
                return
            yield texte


def controler_fichier(chemin: Path, manifeste: dict) -> None:
    """Empreinte et comptes du fichier = ceux du manifeste, avant toute écriture."""
    empreinte, comptes = hashlib.sha256(), {}
    for texte in _lignes_du_fichier(chemin):
        empreinte.update(texte.encode())
        t = json.loads(texte)["t"]
        comptes[t] = comptes.get(t, 0) + 1
    if empreinte.hexdigest() != manifeste["empreinte"]:
        raise ImportRefuse("l'empreinte du fichier n'est pas celle de son manifeste : "
                           "fichier tronqué ou modifié")
    attendus = {t: v["lignes"] for t, v in manifeste["tables"].items() if v.get("lignes")}
    if comptes != attendus:
        raise ImportRefuse(f"comptes du fichier {comptes} ≠ manifeste {attendus}")


def _canonique(ligne: dict) -> int:
    texte = json.dumps(ligne, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(texte.encode()).digest(), "big")


def importer(conn: psycopg.Connection, chemin: Path | str, *,
             stockage: Stockage | None = None, base_publique: str | None = None) -> dict:
    """Verse l'export `chemin` dans la base de `conn` (à `dict_row`, hors transaction)
    et rend, par table, le nombre de lignes et l'empreinte relue sur la cible.

    S'il désigne des objets : `stockage` est le stockage objet de CETTE instance (ses
    identifiants), où l'archive se verse, et `base_publique` la base publique qu'elle
    déclare (`media_store.public_base`), vers laquelle les URL sont réécrites."""
    chemin = Path(chemin)
    manifeste = lire_manifeste(chemin)
    controler_fichier(chemin, manifeste)
    objets = manifeste["objets"]
    if objets["liste"]:
        if stockage is None or not base_publique:
            raise ImportRefuse(f"le fichier désigne {len(objets['liste'])} objet(s) : il "
                               "faut le stockage objet de cette instance et sa base publique")
        archive = chemin.with_name(objets["archive"])
        try:
            controler_archive(archive, objets["empreinte"])
        except ObjetsRefuses as e:
            raise ImportRefuse(str(e)) from e
    cle = _cle_de_l_instance(manifeste)
    bases = (objets["base_publique"], base_publique) if objets["liste"] else None
    with conn.transaction():
        schema = _controler_cible(conn, manifeste)
        # L'import REPRODUIT un état, il ne rejoue pas des gestes : les déclencheurs de
        # la cible (journal des révisions d'une ligne, vecteur de recherche) écriraient
        # une seconde fois ce que le fichier apporte déjà. Suspendus le temps de la
        # transaction — `USER` seulement : les clés étrangères restent vérifiées.
        tables = ["tenants", *manifeste["ordre"]]
        for t in tables:
            conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        attendu = _verser(conn, chemin, manifeste, schema, cle, bases)
        for t in tables:
            conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
        _recaler_sequences(conn, manifeste)
        relu = _relire(conn, manifeste, bases)
        if relu != attendu:
            ecarts = sorted(t for t in set(attendu) | set(relu) if attendu.get(t) != relu.get(t))
            raise VerificationEchouee(f"la cible relue diffère de ce qui a été écrit : {ecarts}")
        if objets["liste"]:
            # Dans la transaction, APRÈS la relecture : un objet qui ne se verse pas
            # annule tout ; ceux déjà versés sont sautés au prochain essai.
            try:
                verser_objets(archive, objets["liste"], stockage, cle)
            except ObjetsRefuses as e:
                raise ImportRefuse(str(e)) from e
    return {t: {"lignes": n, "empreinte": f"{h:064x}"} for t, (n, h) in relu.items()}


def _cle_de_l_instance(manifeste: dict) -> bytes | None:
    """La clé de CETTE instance, si le fichier porte des secrets ou des objets — et c'est
    la leur."""
    if not manifeste["secrets"] and not manifeste["objets"]["liste"]:
        return None
    cle = _load_master_key()
    if cle is None:
        raise ImportRefuse(f"le fichier porte des secrets {manifeste['secrets']} ou des "
                           "objets, et cette instance n'a pas de clé maîtresse "
                           "(OTO_MCP_MASTER_KEY)")
    if empreinte_cle(cle) != manifeste["cle_cible"]:
        raise ImportRefuse("les secrets et objets du fichier sont chiffrés sous une autre "
                           "clé que "
                           "celle de cette instance (empreinte "
                           f"{manifeste['cle_cible'][:12]}… ≠ {empreinte_cle(cle)[:12]}…) : "
                           "refaire l'export avec la clé de CETTE instance")
    return cle


def _controler_cible(conn, manifeste: dict):
    schema = lire_schema(conn)
    verifier_classement(schema, CLASSEMENT)
    version = [r["version_num"] for r in conn.execute("SELECT version_num FROM alembic_version")]
    if version != manifeste["version_schema"]:
        raise ImportRefuse(f"version de schéma cible {version} ≠ source "
                           f"{manifeste['version_schema']} : les deux instances doivent "
                           "servir le même tronc")
    ecarts = [t for t in manifeste["ordre"] if _colonnes(schema, t) != manifeste["colonnes"][t]]
    if ecarts:
        raise ImportRefuse(f"colonnes différentes de la source sur {ecarts}")
    peuplees = [t for t in ("orgs", "users")
                if conn.execute(f"SELECT EXISTS (SELECT 1 FROM {t}) AS e").fetchone()["e"]]
    if peuplees:
        raise ImportRefuse(f"la base cible n'est pas vierge ({peuplees} portent des lignes) : "
                           "l'import vise une base née par le démarrage, rien d'autre")
    primaire = conn.execute("SELECT slug, name FROM tenants WHERE id = 1").fetchone()
    exporte = manifeste["tenant"]
    if primaire is None or primaire["slug"] != exporte["slug"]:
        raise ImportRefuse(f"la cible déclare le tenant primaire "
                           f"{primaire and primaire['slug']!r}, l'export est celui de "
                           f"{exporte['slug']!r} (OTO_TENANT_PRIMAIRE_SLUG)")
    if primaire["name"] != exporte["nom"]:
        raise ImportRefuse(f"le tenant primaire de la cible s'appelle {primaire['name']!r} "
                           f"(OTO_BRAND_NAME), le tenant exporté {exporte['nom']!r} : "
                           "l'instance déclare son nom, l'import ne l'écrase pas")
    return schema


def _verser(conn, chemin: Path, manifeste: dict, schema, cle,
            bases) -> dict[str, tuple[int, int]]:
    transformation = Transformation.depuis(schema, manifeste["comptes"], bases)
    auto = {t: [k.colonnes[0] for k in schema.cles_de(t) if k.cible == t]
            for t in manifeste["ordre"]}
    attendu: dict[str, tuple[int, int]] = {}
    differes: list[tuple[str, dict]] = []
    lot: list[str] = []
    table_du_lot = None
    for texte in _lignes_du_fichier(chemin):
        brut = json.loads(texte)
        t = brut["t"]
        ligne = transformation.appliquer(t, brut["l"])
        if t in AAD and not lisible(t, ligne, cle):
            raise ImportRefuse(f"{t} : un secret ne se déchiffre pas sous la clé de cette "
                               "instance et l'AAD de sa ligne")
        n, h = attendu.get(t, (0, 0))
        attendu[t] = (n + 1, (h + _canonique(ligne)) % _MODULE)
        if t == "tenants":
            _ecrire_tenant_primaire(conn, ligne)
            continue
        if any(ligne.get(c) is not None for c in auto[t]):
            differes.append((t, {c: ligne[c] for c in auto[t]} | _cle(schema, t, ligne)))
            ligne = {**ligne, **{c: None for c in auto[t]}}
        if t != table_du_lot or len(lot) >= TAILLE_LOT:
            _inserer(conn, table_du_lot, lot)
            lot, table_du_lot = [], t
        lot.append(json.dumps(ligne))
    _inserer(conn, table_du_lot, lot)
    # Les auto-références (une page sous une page) se posent quand toutes les lignes
    # de la table sont là : leur ordre d'insertion n'a pas à connaître l'arbre.
    for t, v in differes:
        cle_ligne = _cle(schema, t, v)
        poses = [c for c in v if c not in cle_ligne]
        conn.execute(f"UPDATE {t} SET " + ", ".join(f"{c} = %({c})s" for c in poses)
                     + " WHERE " + " AND ".join(f"{c} = %({c})s" for c in cle_ligne), v)
    return attendu


def _inserer(conn, t: str | None, lot: list[str]) -> None:
    if not lot:
        return
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO {t} SELECT * FROM json_populate_record(NULL::{t}, "
                        "%s::json)", [(x,) for x in lot])


def _cle(schema, t: str, ligne: dict) -> dict:
    return {c: ligne[c] for c in schema.primaires[t]}


def _ecrire_tenant_primaire(conn, ligne: dict) -> None:
    """La ligne 1 semée prend les valeurs du tenant exporté (slug et nom déjà égaux)."""
    cols = [c for c in ligne if c != "id"]
    conn.execute(f"UPDATE tenants SET ({', '.join(cols)}) = (SELECT {', '.join(cols)} "
                 "FROM json_populate_record(NULL::tenants, %s::json)) WHERE id = 1",
                 (json.dumps(ligne),))


def _recaler_sequences(conn, manifeste: dict) -> None:
    for cle, maximum in manifeste["sequences"].items():
        t, c = cle.split(".")
        conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}', '{c}'), "
                     f"GREATEST(%s, (SELECT max({c}) FROM {t})))", (maximum,))


def _relire(conn, manifeste: dict, bases) -> dict[str, tuple[int, int]]:
    """Relit le périmètre sur la cible ; refuse s'il y subsiste une URL de NOTRE
    stockage public — la réécriture ne doit rien avoir laissé derrière elle."""
    lu = ouvrir(conn, manifeste["perimetre"]["orgs_declarees"])
    relu: dict[str, tuple[int, int]] = {}
    restes: dict[str, int] = {}
    for t in lu.ordre:
        for texte in lu.lignes(conn, t):
            n, h = relu.get(t, (0, 0))
            relu[t] = (n + 1, (h + _canonique(json.loads(texte))) % _MODULE)
            if bases and f"{bases[0]}/" in texte:
                restes[t] = restes.get(t, 0) + 1
    if restes:
        raise VerificationEchouee(f"des URL de l'ancien stockage public subsistent : {restes}")
    return relu
