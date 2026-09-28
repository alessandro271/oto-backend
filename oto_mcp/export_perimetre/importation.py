"""L'import d'un export de périmètre dans une base NÉE PAR LE DÉMARRAGE (#969).

La cible est une base vierge qu'`init_db` a montée pour l'instance qui la sert : même
schéma, version Alembic posée, tenant primaire (ligne 1) semé depuis
`OTO_TENANT_PRIMAIRE_SLUG`. L'import y verse le fichier en UNE transaction, et ne la
valide qu'après s'être relu.

Refus, tous AVANT la première écriture et chacun nommé (`ImportRefuse`) : fichier dont
l'empreinte ou les comptes ne sont pas ceux du manifeste, schéma ou version
différents de la source, base cible déjà peuplée, tenant primaire de la cible qui
n'est pas celui de l'export, secrets à rechiffrer sans les deux clés.

Ce qui change en chemin, et rien d'autre :
- **le tenant** exporté devient la ligne 1 de la cible (la ligne semée prend ses
  valeurs, son slug est déjà le même) ; toute clé étrangère vers `tenants(id)` vaut 1 ;
- **les comptes** perdent le préfixe `<slug>:` que leur donnait un tenant tiers
  (`manifeste["comptes"]`) : sur la cible, le tenant est primaire et ses subs sont nus.
  Le remplacement vise les VALEURS exactement égales à un sub du périmètre, ou à sa
  forme membre `<org>:<sub>`, à toute profondeur d'un JSON ;
- **les secrets** sont rechiffrés de la clé source vers la clé cible (`rechiffrement`).

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

from .classement import CLASSEMENT
from .decouverte import lire_schema, verifier_classement
from .extraction import FORMAT, _colonnes, ouvrir
from .rechiffrement import AAD, Cles, rechiffrer

_MODULE = 2 ** 256


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


def _denuder(v, comptes: dict[str, str]):
    """Le sub cible de chaque valeur qui EST un sub du périmètre (ou sa forme membre)."""
    if isinstance(v, str):
        if v in comptes:
            return comptes[v]
        tete, sep, reste = v.partition(":")
        if sep and tete.isdigit() and reste in comptes:
            return f"{tete}:{comptes[reste]}"
        return v
    if isinstance(v, list):
        return [_denuder(x, comptes) for x in v]
    if isinstance(v, dict):
        return {k: _denuder(x, comptes) for k, x in v.items()}
    return v


def importer(conn: psycopg.Connection, chemin: Path | str, *, cles: Cles | None = None) -> dict:
    """Verse l'export `chemin` dans la base de `conn` (à `dict_row`, hors transaction)
    et rend, par table, le nombre de lignes et l'empreinte relue sur la cible."""
    chemin = Path(chemin)
    manifeste = lire_manifeste(chemin)
    controler_fichier(chemin, manifeste)
    if manifeste["secrets"] and cles is None:
        raise ImportRefuse(f"secrets à rechiffrer {manifeste['secrets']} : les deux clés "
                           "(source et cible) sont requises")
    with conn.transaction():
        schema = _controler_cible(conn, manifeste)
        # L'import REPRODUIT un état, il ne rejoue pas des gestes : les déclencheurs de
        # la cible (journal des révisions d'une ligne, vecteur de recherche) écriraient
        # une seconde fois ce que le fichier apporte déjà. Suspendus le temps de la
        # transaction — `USER` seulement : les clés étrangères restent vérifiées.
        tables = ["tenants", *manifeste["ordre"]]
        for t in tables:
            conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        attendu = _verser(conn, chemin, manifeste, schema, cles)
        for t in tables:
            conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
        _recaler_sequences(conn, manifeste)
        relu = _relire(conn, manifeste)
        if relu != attendu:
            ecarts = sorted(t for t in set(attendu) | set(relu) if attendu.get(t) != relu.get(t))
            raise VerificationEchouee(f"la cible relue diffère de ce qui a été écrit : {ecarts}")
    return {t: {"lignes": n, "empreinte": f"{h:064x}"} for t, (n, h) in relu.items()}


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
    slug = conn.execute("SELECT slug FROM tenants WHERE id = 1").fetchone()
    if slug is None or slug["slug"] != manifeste["tenant"]["slug"]:
        raise ImportRefuse(f"la cible déclare le tenant primaire "
                           f"{slug and slug['slug']!r}, l'export est celui de "
                           f"{manifeste['tenant']['slug']!r} (OTO_TENANT_PRIMAIRE_SLUG)")
    return schema


def _verser(conn, chemin: Path, manifeste: dict, schema, cles) -> dict[str, tuple[int, int]]:
    comptes = {s: c for s, c in manifeste["comptes"].items() if s != c}
    vers_tenant = {k.table: k.colonnes for k in schema.cles
                   if k.cible == "tenants" and k.colonnes_cible == ("id",)}
    auto = {t: [k.colonnes[0] for k in schema.cles_de(t) if k.cible == t]
            for t in manifeste["ordre"]}
    attendu: dict[str, tuple[int, int]] = {}
    differes: list[tuple[str, dict]] = []
    for texte in _lignes_du_fichier(chemin):
        brut = json.loads(texte)
        t, source = brut["t"], brut["l"]
        ligne = _denuder(source, comptes) if comptes else dict(source)
        for c in vers_tenant.get(t, ()):
            if ligne[c] is not None:
                ligne[c] = 1
        if t == "tenants":
            ligne["id"] = 1
        if t in AAD and cles is not None:
            rechiffrer(t, source, ligne, cles)
        n, h = attendu.get(t, (0, 0))
        attendu[t] = (n + 1, (h + _canonique(ligne)) % _MODULE)
        if t == "tenants":
            _ecrire_tenant_primaire(conn, ligne)
            continue
        if any(ligne.get(c) is not None for c in auto[t]):
            differes.append((t, {c: ligne[c] for c in auto[t]} | _cle(schema, t, ligne)))
            ligne = {**ligne, **{c: None for c in auto[t]}}
        conn.execute(f"INSERT INTO {t} SELECT * FROM json_populate_record(NULL::{t}, %s::json)",
                     (json.dumps(ligne),))
    # Les auto-références (une page sous une page) se posent quand toutes les lignes
    # de la table sont là : leur ordre d'insertion n'a pas à connaître l'arbre.
    for t, v in differes:
        cle = _cle(schema, t, v)
        poses = [c for c in v if c not in cle]
        conn.execute(f"UPDATE {t} SET " + ", ".join(f"{c} = %({c})s" for c in poses)
                     + " WHERE " + " AND ".join(f"{c} = %({c})s" for c in cle), v)
    return attendu


def _cle(schema, t: str, ligne: dict) -> dict:
    return {c: ligne[c] for c in schema.primaires[t]}


def _ecrire_tenant_primaire(conn, ligne: dict) -> None:
    cols = [c for c in ligne if c != "id"]
    conn.execute(f"UPDATE tenants SET ({', '.join(cols)}) = (SELECT {', '.join(cols)} "
                 "FROM json_populate_record(NULL::tenants, %s::json)) WHERE id = 1",
                 (json.dumps(ligne),))


def _recaler_sequences(conn, manifeste: dict) -> None:
    for cle, maximum in manifeste["sequences"].items():
        t, c = cle.split(".")
        conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}', '{c}'), "
                     f"GREATEST(%s, (SELECT max({c}) FROM {t})))", (maximum,))


def _relire(conn, manifeste: dict) -> dict[str, tuple[int, int]]:
    lu = ouvrir(conn, manifeste["perimetre"]["orgs_declarees"])
    relu: dict[str, tuple[int, int]] = {}
    for t in lu.ordre:
        for texte in lu.lignes(conn, t):
            n, h = relu.get(t, (0, 0))
            relu[t] = (n + 1, (h + _canonique(json.loads(texte))) % _MODULE)
    return relu
