"""Les lignes du périmètre qui portent le compte d'un ANCIEN membre : rattacher, sinon omettre.

Le périmètre dérive ses comptes des MEMBRES de ses orgs (`perimetre`). Une table
possédée par org porte pourtant aussi des lignes écrites par des comptes qui n'en sont
plus membres : sur une vraie base (répétition à blanc, #1088), d'anciens comptes de
l'annuaire du tenant primaire (sub NU), d'avant que le tenant tiers ait son propre
annuaire, et pour la moitié d'entre eux un compte `<slug>:<même id>` du périmètre. Sans
règle, la `Transformation` dénude le jumeau en `<id>`, qui heurte les lignes de l'ancien
compte `<id>` : l'import tombait sur une violation d'unicité brute au bout de deux
minutes, et les lignes sans jumeau désignaient sur la cible un compte qui n'y existe pas.

La règle (décidée) se tranche et se compte À L'EXPORT. Pour chaque colonne-compte
(`classement.comptes_de`) d'une ligne du périmètre, la valeur `v` est :

- NULL ou un compte du périmètre : rien ne change ;
- un sub NU (aucun préfixe de tenant tiers de la source, `tenancy.tenant_of`) dont
  `<slug>:v` est un compte du périmètre : la ligne est RATTACHÉE à ce jumeau. Elle part
  telle quelle — sur la cible, le jumeau perd son préfixe et porte `v`. Si elle y
  doublonnerait une ligne du jumeau sur une clé unique, le jumeau gagne : elle est OMISE ;
- un sub nu sans jumeau : la ligne est OMISE ;
- toute autre valeur (un compte d'un AUTRE tenant, ou `<slug>:` hors périmètre) :
  REFUS nommé (`ComptesHorsRegle`), jamais une erreur de base à l'import.

Le prédicat (`garde`) entre dans celui de la table (`extraction.compilateur`) : l'écriture,
les enfants (`Via`), le contrôle de fermeture, les objets, les séquences et la relecture
de l'import lisent les mêmes lignes. Sur la cible il ne retire rien : tous les comptes y
sont du périmètre, et le fichier n'en porte pas d'autre. Le manifeste compte, par table,
les lignes rattachées, omises en doublon, omises sans jumeau et omises avec leur parent,
et le nombre de comptes concernés — jamais leurs identifiants (`recenser`).
"""
from __future__ import annotations

import re
from typing import Callable

from .classement import comptes_de
from .decouverte import Schema, Unique
from .regles import Compte

_JUMEAU = "(%(prefixe_tenant)s::text || {v})"


class ComptesHorsRegle(RuntimeError):
    """Des lignes du périmètre désignent un compte que la règle ne rattache ni n'omet."""

    def __init__(self, comptes: dict[str, int]):
        self.comptes = comptes
        detail = "; ".join(f"{k} : {n} ligne(s)" for k, n in sorted(comptes.items()))
        super().__init__("des lignes du périmètre désignent un compte hors périmètre qui "
                         "n'est ni un sub nu de l'annuaire primaire, ni le jumeau "
                         f"`<slug>:` d'un compte du périmètre — à trancher : {detail}")


def _nu(v: str) -> str:
    return ("NOT EXISTS (SELECT 1 FROM unnest(%(prefixes_tiers)s::text[]) pt(prefixe) "
            f"WHERE starts_with({v}, pt.prefixe))")


def _hors(v: str) -> str:
    return f"NOT ({v} = ANY(%(subs)s))"


def du_perimetre(v: str) -> str:
    return f"({v} IS NULL OR {v} = ANY(%(subs)s))"


def rattachable(v: str) -> str:
    return (f"COALESCE({_hors(v)} AND {_nu(v)} AND "
            f"{_JUMEAU.format(v=v)} = ANY(%(subs)s), false)")


def sans_jumeau(v: str) -> str:
    return (f"COALESCE({_hors(v)} AND {_nu(v)} AND NOT COALESCE("
            f"{_JUMEAU.format(v=v)} = ANY(%(subs)s), false), false)")


def hors_regle(v: str) -> str:
    return f"COALESCE({_hors(v)} AND NOT {_nu(v)}, false)"


def rattache(v: str) -> str:
    """Le compte SOURCE auquel la valeur `v` se rattache : son jumeau, ou elle-même."""
    return f"(CASE WHEN {rattachable(v)} THEN {_JUMEAU.format(v=v)} ELSE {v} END)"


def garde(schema: Schema, t: str, comptes: tuple[Compte, ...], appartient: str) -> str:
    """Le prédicat « les comptes de la ligne sont du périmètre, ou rattachés sans
    doublon » ; `appartient` est celui de la table sans lui (règle et destinataire)."""
    admis = " AND ".join(f"({du_perimetre(k.valeur)} OR {rattachable(k.valeur)})"
                         for k in comptes)
    doublon = _doublon(schema, t, comptes, appartient)
    if doublon is None:
        return f"({admis})"
    un_rattache = " OR ".join(rattachable(k.valeur) for k in comptes)
    return f"(({admis}) AND (CASE WHEN {un_rattache} THEN NOT ({doublon}) ELSE true END))"


def _mots(sql: str) -> set[str]:
    return set(re.findall(r"[a-z_][a-z0-9_]*", sql))


def _doublon(schema: Schema, t: str, comptes: tuple[Compte, ...],
             appartient: str) -> str | None:
    """La ligne rattachée heurterait-elle, sur une clé unique qui lit une colonne-compte,
    une ligne du jumeau qui part ? Chaque clé s'évalue telle que le catalogue la donne
    (expression, prédicat partiel compris) sur la ligne où le compte est remplacé par
    son jumeau : c'est la forme qu'elle aurait sur la cible, à la dénudation près."""
    par_colonne = {k.colonne: k for k in comptes}
    uniques = [u for u in schema.uniques.get(t, ())
               if _mots(" ".join((*u.cles, u.predicat or ""))) & set(par_colonne)]
    if not uniques:
        return None

    def colonne(c: str) -> str:
        k = par_colonne.get(c)
        if k is None:
            return f'"{c}"'
        return (f"CASE WHEN {rattachable(k.valeur)} THEN "
                f"{k.remplacer(_JUMEAU.format(v=k.valeur))} ELSE \"{c}\" END")

    substituee = ", ".join(f'{colonne(c)} AS "{c}"' for c in schema.colonnes[t])
    jumeau = " AND ".join(du_perimetre(k.valeur) for k in comptes)
    return " OR ".join(_heurte(t, u, substituee, appartient, jumeau) for u in uniques)


def _heurte(t: str, u: Unique, substituee: str, appartient: str, jumeau: str) -> str:
    partiel = u.predicat or "true"
    egal = "IS NOT DISTINCT FROM" if u.nulls_egaux else "="
    cles = ", ".join(f"{c} AS __k{i}" for i, c in enumerate(u.cles))
    memes = " AND ".join(f"{c} {egal} r.__k{i}" for i, c in enumerate(u.cles))
    # La sous-requête sans FROM lit la ligne examinée ; les clés et le prédicat, écrits
    # sur les noms nus des colonnes, s'évaluent sur elle (r0) puis sur les lignes du
    # jumeau (la table, au niveau le plus intérieur).
    return (f"EXISTS (SELECT 1 FROM (SELECT {cles}, ({partiel}) AS __p "
            f"FROM (SELECT {substituee}) r0) r WHERE r.__p AND EXISTS (SELECT 1 FROM {t} "
            f"WHERE ({appartient}) AND {jumeau} AND ({partiel}) AND {memes}))")


def recenser(conn, lu, appartient: Callable[[str], str]) -> dict:
    """Ce que la règle fait des lignes du périmètre, compté pour le manifeste ; lève
    `ComptesHorsRegle` (table, colonne, nombre) avant toute écriture si une valeur sort
    de la règle. Les identifiants des comptes concernés ne sont pas rendus."""
    tables: dict[str, dict[str, int]] = {}
    hors: dict[str, int] = {}
    rattaches: set[str] = set()
    orphelins: set[str] = set()

    def compter(sql: str) -> dict:
        return conn.execute(sql, lu.params).fetchone()

    for t in lu.ordre:
        base = appartient(t)
        comptes = comptes_de(lu.classement[t])
        compte: dict[str, int] = {}
        if comptes:
            for k in comptes:
                v = k.valeur
                r = compter(f"SELECT count(*) FILTER (WHERE {hors_regle(v)}) AS hors, "
                            f"array_agg(DISTINCT {v}) FILTER (WHERE {rattachable(v)}) AS r, "
                            f"array_agg(DISTINCT {v}) FILTER (WHERE {sans_jumeau(v)}) AS o "
                            f"FROM {t} WHERE {base}")
                if r["hors"]:
                    hors[f"{t}.{k.nom}"] = r["hors"]
                rattaches |= set(r["r"] or ())
                orphelins |= set(r["o"] or ())
            un_rattache = " OR ".join(rattachable(k.valeur) for k in comptes)
            un_orphelin = " OR ".join(sans_jumeau(k.valeur) for k in comptes)
            compte = dict(compter(
                f"SELECT count(*) FILTER (WHERE ({lu.pred(t)}) AND ({un_rattache})) "
                f"AS rattachees, count(*) FILTER (WHERE NOT ({lu.pred(t)}) AND "
                f"({un_rattache}) AND NOT ({un_orphelin})) AS omises_doublon, "
                f"count(*) FILTER (WHERE {un_orphelin}) AS omises_sans_jumeau "
                f"FROM {t} WHERE {base}"))
        brut = lu.brut(t)
        if brut != base:
            # Un enfant (`Via`) suit son parent : omis avec lui, compté ici.
            compte["omises_avec_leur_parent"] = (
                compter(f"SELECT count(*) AS n FROM {t} WHERE {brut}")["n"]
                - compter(f"SELECT count(*) AS n FROM {t} WHERE {base}")["n"])
        compte = {k: n for k, n in compte.items() if n}
        if compte:
            tables[t] = compte
    if hors:
        raise ComptesHorsRegle(hors)
    return {"tables": tables,
            "comptes": {"rattaches": len(rattaches), "sans_jumeau": len(orphelins)}}
