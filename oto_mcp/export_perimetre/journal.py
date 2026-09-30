"""Le journal hors fenêtre : le journal d'appels voyage à part, par tranches de dates (#1088).

Mesuré sur une vraie copie de production : 4,33 M des 4,47 M lignes d'un périmètre sont
des appels du journal (`classement.JOURNAL`), et l'essentiel des ~35 min d'export et des
~64 min d'import. La fenêtre de coupure en tient 30 à 60. Le journal sort donc de la
fenêtre : l'export principal le laisse (`extraction.exporter(journal=False)`), et il se
verse par TRANCHES `[depuis, jusqu_a)` sur sa colonne d'horodatage, avant et après.

Une tranche est un export comme les autres, par le même déroulé
(`extraction.exporter_lecture`) : même instantané `REPEATABLE READ READ ONLY`, même
périmètre, même règle des anciens comptes (rattacher, sinon omettre, comptée), même
`Transformation`, même rechiffrement et même archive des objets qu'il cite, son propre
manifeste (format `FORMAT_TRANCHE`, bornes, empreinte). Seule la lecture change
(`tranche`) : le journal seul, ses prédicats bornés à la fenêtre. Avec
`faits_de_run=True`, elle emporte en plus TOUS les faits de run antérieurs à `depuis`
(`classement.FAITS_DE_RUN`) : ils sont la source de vérité des runs.

Son import (`importer_tranche`) vise une instance NÉE par le démarrage, que l'import
principal ait eu lieu ou non : la tranche se pousse la veille dans une base sans orgs ni
comptes. Il vérifie le schéma et le tenant primaire (slug et nom), prend le périmètre
dans le manifeste (`perimetre_du`) — il n'exige ni les orgs ni les comptes sur la cible —,
écrit par lots en UNE transaction, sans doublon (`ON CONFLICT` sur la clé primaire : une
tranche rejouée, ou deux tranches qui se chevauchent, n'insèrent rien deux fois), relit
la fenêtre sur la cible (nombre et empreinte des lignes du périmètre) et annule tout en
cas d'écart. Il ne touche à aucune autre table ; la séquence du journal ne fait que
monter (`importation.avancer_sequence`).
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .classement import CLASSEMENT, FAITS_DE_RUN, JOURNAL
from .decouverte import verifier_journal
from .extraction import Lecture, exporter_lecture, lire, ouvrir
from .importation import (ImportRefuse, cle_de_l_instance, comparer, controler_fichier,
                          controler_schema, controler_tenant, lignes_cibles, lire_manifeste,
                          par_lots, preparer_objets, recaler_sequences, relire,
                          verser_archive)
from .objets import Stockage
from .perimetre import Perimetre, prefixes_tiers

FORMAT_TRANCHE = "oto-export-perimetre-journal/1"


class TrancheRefusee(RuntimeError):
    """Les bornes d'une tranche ne désignent pas une fenêtre."""


def instant(texte: str) -> datetime:
    """Une borne en ISO 8601 (`2026-10-01`, `2026-10-01T18:00:00+02:00`) ; sans fuseau,
    elle est en UTC — comme les horodatages de l'export."""
    try:
        valeur = datetime.fromisoformat(texte)
    except ValueError as e:
        raise TrancheRefusee(f"borne {texte!r} : pas une date ISO 8601") from e
    return valeur if valeur.tzinfo else valeur.replace(tzinfo=timezone.utc)


def tranche(lu: Lecture, depuis: datetime, jusqu_a: datetime,
            faits_de_run: bool = False) -> Lecture:
    """La lecture du journal seul, bornée à `[depuis, jusqu_a)` sur son horodatage (et,
    avec `faits_de_run`, étendue aux faits de run antérieurs à `depuis`). La règle et la
    règle des comptes restent celles de la table entière : une tranche ne lit que moins
    de lignes, jamais d'autres."""
    if not depuis < jusqu_a:
        raise TrancheRefusee(f"tranche vide ou renversée : [{depuis.isoformat()}, "
                             f"{jusqu_a.isoformat()})")

    def borner(pred):
        def borne(t: str) -> str:
            h = JOURNAL[t]
            fenetre = f"{h} >= %(depuis)s"
            if faits_de_run and t in FAITS_DE_RUN:
                fenetre = f"({fenetre} OR {FAITS_DE_RUN[t][0]} = ANY(%(faits_{t})s))"
            return f"({pred(t)}) AND {h} < %(jusqu_a)s AND {fenetre}"
        return borne

    params = {**lu.params, "depuis": depuis, "jusqu_a": jusqu_a,
              **{f"faits_{t}": list(v) for t, (_, v) in FAITS_DE_RUN.items()}}
    return replace(lu, pred=borner(lu.pred), brut=borner(lu.brut),
                   appartient=borner(lu.appartient), params=params,
                   ordre=[t for t in lu.ordre if t in JOURNAL])


def _bornes(depuis: datetime, jusqu_a: datetime, faits_de_run: bool) -> dict:
    return {"depuis": depuis.isoformat(), "jusqu_a": jusqu_a.isoformat(),
            "horodatage": dict(JOURNAL), "faits_de_run_complets": faits_de_run}


def exporter_tranche(conn: psycopg.Connection, orgs: list[int], sortie: Path | str, *,
                     depuis: datetime, jusqu_a: datetime, base_publique: str,
                     cle_cible: bytes | None = None, stockage: Stockage | None = None,
                     faits_de_run: bool = False) -> dict:
    """Exporte les appels du journal du périmètre des `orgs` sur `[depuis, jusqu_a)` vers
    `sortie`, et rend le manifeste. Mêmes entrées et mêmes refus que
    `extraction.exporter` ; le manifeste dit en plus la tranche et, pour vérifier qu'une
    dernière tranche bornée par le gel n'a rien laissé, les lignes du périmètre au-delà
    (`apres`)."""
    entiere: dict[str, Lecture] = {}

    def ouvrir_tranche(conn) -> Lecture:
        lu = ouvrir(conn, orgs, CLASSEMENT)
        verifier_journal(lu.schema, lu.classement)
        entiere["lu"] = lu
        return tranche(lu, depuis, jusqu_a, faits_de_run)

    def completer(conn, lu: Lecture, manifeste: dict) -> None:
        pleine = entiere["lu"]
        manifeste["format"] = FORMAT_TRANCHE
        manifeste["tranche"] = _bornes(depuis, jusqu_a, faits_de_run) | {"apres": {
            t: conn.execute(f"SELECT count(*) AS n FROM {t} WHERE ({pleine.pred(t)}) "
                            f"AND {h} >= %(jusqu_a)s", lu.params).fetchone()["n"]
            for t, h in JOURNAL.items()}}

    return exporter_lecture(conn, sortie, ouvrir_tranche, completer,
                            base_publique=base_publique, cle_cible=cle_cible,
                            stockage=stockage)


def perimetre_du(conn, manifeste: dict) -> Perimetre:
    """Le périmètre de la tranche SUR LA CIBLE, lu dans le manifeste : ses orgs, ses
    comptes (déjà nus) et le tenant primaire. Rien n'y est résolu — la cible peut ne
    porter encore ni orgs ni comptes (la tranche est poussée avant l'import principal),
    ou en porter de nouveaux (une tranche versée après la bascule)."""
    p = manifeste["perimetre"]
    orgs = tuple(sorted({*p["orgs_declarees"], *p["orgs_personnelles"]}))
    groupes = tuple(r["id"] for r in conn.execute(
        "SELECT id FROM org_groups WHERE org_id = ANY(%s) ORDER BY id", (list(orgs),)))
    return Perimetre(tuple(p["orgs_declarees"]), orgs, groupes,
                     tuple(sorted(set(manifeste["comptes"].values()))), 1,
                     manifeste["tenant"]["slug"], True, prefixes_tiers(conn))


def _controler_tranche(conn, manifeste: dict) -> None:
    """Le fichier ne porte que le journal, et le journal de la cible n'a pas de
    déclencheur : une tranche se verse dans une instance qui peut servir, sans suspendre
    ses déclencheurs (ce qui verrouillerait la table le temps de la transaction)."""
    autres = sorted(set(manifeste["ordre"]) - set(JOURNAL))
    if autres:
        raise ImportRefuse(f"une tranche du journal ne porte que {sorted(JOURNAL)} ; "
                           f"celle-ci porte aussi {autres}")
    declencheurs = [f"{r['t']}.{r['nom']}" for r in conn.execute(
        "SELECT tgrelid::regclass::text AS t, tgname AS nom FROM pg_trigger "
        "WHERE NOT tgisinternal AND tgenabled <> 'D' AND tgrelid = ANY(%s::regclass[]) "
        "ORDER BY 1, 2", (manifeste["ordre"],))]
    if declencheurs:
        raise ImportRefuse(f"le journal de la cible porte des déclencheurs {declencheurs} : "
                           "une tranche reproduit des appels, elle ne les rejoue pas")


def importer_tranche(conn: psycopg.Connection, chemin: Path | str, *,
                     stockage: Stockage | None = None,
                     base_publique: str | None = None) -> dict:
    """Verse la tranche `chemin` dans la base de `conn` (à `dict_row`, hors transaction)
    et rend, par table, les lignes de la tranche, celles insérées, celles déjà présentes,
    et l'empreinte relue sur la cible. Rejouée, elle n'insère rien."""
    chemin = Path(chemin)
    manifeste = lire_manifeste(chemin, FORMAT_TRANCHE)
    controler_fichier(chemin, manifeste)
    archive, bases = preparer_objets(chemin, manifeste, stockage, base_publique)
    cle = cle_de_l_instance(manifeste)
    bornes = manifeste["tranche"]
    with conn.transaction():
        schema = controler_schema(conn, manifeste)
        controler_tenant(conn, manifeste)
        _controler_tranche(conn, manifeste)
        lu = lire(conn, perimetre_du(conn, manifeste), CLASSEMENT)
        verifier_journal(lu.schema, lu.classement)
        attendu: dict[str, tuple[int, int]] = {}
        inserees: dict[str, int] = {}
        for t, lot in par_lots(lignes_cibles(chemin, manifeste, schema, cle, bases,
                                             attendu)):
            inserees[t] = inserees.get(t, 0) + _inserer_sans_doublon(conn, schema,
                                                                     manifeste, t, lot)
        recaler_sequences(conn, manifeste)
        relu = relire(conn, tranche(lu, instant(bornes["depuis"]), instant(bornes["jusqu_a"]),
                                    bornes["faits_de_run_complets"]), bases)
        comparer(attendu, relu)
        verser_archive(archive, manifeste, stockage, cle)
    return {t: {"lignes": n, "inserees": inserees.get(t, 0),
                "deja_presentes": n - inserees.get(t, 0), "empreinte": f"{h:064x}"}
            for t, (n, h) in relu.items()}


def _inserer_sans_doublon(conn, schema, manifeste: dict, t: str, lot: list[dict]) -> int:
    """Un lot en une requête ; une ligne dont la clé primaire est déjà là n'est pas
    insérée (rejouée, ou d'un chevauchement). Rend le nombre de lignes insérées. Une clé
    déjà prise par une AUTRE ligne se voit à la relecture, qui annule tout."""
    colonnes = ", ".join(manifeste["colonnes"][t])
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO {t} ({colonnes}) SELECT {colonnes} FROM "
                    f"json_populate_recordset(NULL::{t}, %s::json) "
                    f"ON CONFLICT ({', '.join(schema.primaires[t])}) DO NOTHING",
                    (json.dumps(lot),))
        return cur.rowcount
