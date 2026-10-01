"""oto-backend#980 (suite) — une page de lignes voyage en UN message PostgreSQL.

Ligne à ligne, libpq lit le résultat par tranches de ~10-16 Ko et psycopg rend le
GIL à CHAQUE tranche pour attendre la socket. Seul, c'est gratuit ; à côté d'un
thread qui calcule, chaque reprise du GIL attend jusqu'à `sys.getswitchinterval()`
(le convoi de `docs/event-loop-perf.md`). Reproduit en local sur un tableau de
8 900 lignes × 90 colonnes : un ramassage complet par la route REST passait de
~2 s à ~11 s à côté d'un seul thread de calcul, intervalle à 1 ms compris.

⚠️ Banc de COMPTE, jamais de durée : il compte les attentes de socket de psycopg
(chacune = un GIL rendu puis repris), ce qui ne dépend pas de la machine. Le
contrôle qui mord : la même page lue ligne à ligne en coûte des dizaines.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")

SUB = "usr_page_un_message_980"
N_LIGNES = 300
PAGE = 250
COLS = ("row_id, created_at, updated_at, data, rev, claimed_by, claimed_until, "
        "claimed_run, claims, abandon_reason, "
        "(claimed_until IS NOT NULL AND claimed_until > NOW()) AS claim_active")


@pytest.fixture(scope="module")
def vivier(live):
    """Un tableau de lignes GROSSES (~4 Ko de JSON chacune, ~1 Mo par page) et
    variées : couches, flottants, texte non ASCII, réservations actives et échues,
    horodatages à microsecondes (la forme servie les tronque)."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    db.upsert_user(SUB, email=f"{SUB}@page980.invalid", name=SUB)
    ns_id = db.create_datastore("user", SUB, "t980um-" + uuid.uuid4().hex[:6])
    with _connect() as conn:
        for i in range(N_LIGNES):
            data = {f"c{j:02d}": os.urandom(20).hex() for j in range(45)}
            data["statut"] = ["neuf", "vu", "écarté"][i % 3]
            data["score"] = i / 7
            data["email"] = {"valeur": f"x{i}@exemple.invalid", "origine": "import"}
            conn.execute(
                "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at, "
                "claimed_by, claimed_until, claimed_run, claims) "
                "VALUES (%s, %s, %s::jsonb, "
                "'2026-09-18 10:11:12.987654+00'::timestamptz + %s * interval '1 second', "
                "'2026-09-19 08:00:00.5+00', %s, "
                "CASE WHEN %s THEN NOW() + interval '1 hour' "
                "     WHEN %s THEN NOW() - interval '1 hour' END, %s, %s)",
                (ns_id, f"r-{i:04d}", json.dumps(data), i // 2,
                 "agent" if i % 5 == 0 else None, i % 10 == 0, i % 10 == 5,
                 "run-x" if i % 5 == 0 else None, i % 4),
            )
    return ns_id


def _reference(sql: str, params: tuple) -> list[dict]:
    """La page lue LIGNE À LIGNE — la forme d'avant, qui fait foi."""
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


@contextmanager
def _attentes():
    """Compte les attentes de socket de psycopg (un GIL rendu à chacune)."""
    import psycopg.waiting as waiting

    vrai = waiting.wait
    compte = [0]

    def compte_les_attentes(gen):
        try:
            etat = next(gen)
            while True:
                compte[0] += 1
                pret = yield etat
                etat = gen.send(pret)
        except StopIteration as fin:
            return fin.value

    def wait(gen, *a, **kw):
        return vrai(compte_les_attentes(gen), *a, **kw)

    waiting.wait = wait
    try:
        yield compte
    finally:
        waiting.wait = vrai


def test_liste_historique_identique_a_la_lecture_ligne_a_ligne(vivier):
    from oto_mcp import db

    rows = db.datastore_list_rows(vivier, offset=10, limit=40, order_by="_created_at",
                                  order_dir="asc")
    ref = _reference(f"SELECT {COLS} FROM datastore_rows WHERE ns_id = %s "
                     "ORDER BY created_at ASC, row_id ASC LIMIT 40 OFFSET 10", (vivier,))
    assert rows == ref
    assert rows[0]["created_at"] == ref[0]["created_at"]
    assert isinstance(rows[0]["created_at"], str) and "." not in rows[0]["created_at"]
    assert {r["claim_active"] for r in rows} == {True, False}


def test_liste_triee_par_colonne_identique(vivier):
    """Tri d'une colonne utilisateur : ses paramètres passent par la clause `WINDOW`,
    dans l'ordre positionnel d'avant."""
    from oto_mcp import db

    rows = db.datastore_list_rows(vivier, offset=0, limit=30, order_by="statut",
                                  order_dir="desc", filters=[{"field": "statut", "op": "ne",
                                                              "value": "vu"}])
    ref = _reference(f"SELECT {COLS} FROM datastore_rows WHERE ns_id = %s "
                     "AND data->>'statut' <> 'vu' "
                     "ORDER BY data->>'statut' DESC, row_id DESC LIMIT 30", (vivier,))
    assert rows == ref


def test_curseur_identique(vivier):
    from oto_mcp import db

    rows = db.datastore_list_rows_after(vivier, after_row_id="r-0100", limit=25)
    ref = _reference(f"SELECT {COLS} FROM datastore_rows WHERE ns_id = %s "
                     "AND row_id > %s ORDER BY row_id ASC LIMIT 25", (vivier, "r-0100"))
    assert rows == ref


def test_page_et_statistiques_identiques(vivier):
    from oto_mcp import db

    filtres = [{"field": "statut", "op": "eq", "value": "neuf"}]
    rows, total, off_type, empty = db.datastore_page_with_stats(
        vivier, offset=5, limit=20, order_by="_id", order_dir="asc", filters=filtres)
    assert total == N_LIGNES // 3 and (off_type, empty) == (0, 0)
    ref = _reference(f"SELECT {COLS} FROM datastore_rows WHERE ns_id = %s "
                     "AND data->>'statut' = 'neuf' ORDER BY row_id ASC LIMIT 20 OFFSET 5",
                     (vivier,))
    assert rows == ref


def test_page_au_dela_du_total_garde_le_total(vivier):
    from oto_mcp import db

    rows, total, _o, _e = db.datastore_page_with_stats(
        vivier, offset=10_000, limit=20, order_by="_id", order_dir="asc",
        filters=[{"field": "statut", "op": "eq", "value": "vu"}])
    assert rows == [] and total == N_LIGNES // 3


def test_une_page_coute_une_poignee_d_attentes_pas_une_par_tranche(vivier):
    """Le cœur du lot. La lecture ligne à ligne de la même page (~1 Mo) coûte des
    dizaines d'attentes — le contrôle qui prouve que le compteur voit quelque chose ;
    chacune des quatre lectures servies doit en coûter au plus le quart (rapport
    relatif : le nombre absolu dépend de la machine)."""
    from oto_mcp import db

    with _attentes() as ligne_a_ligne:
        ref = _reference(f"SELECT {COLS} FROM datastore_rows WHERE ns_id = %s "
                         "ORDER BY row_id ASC LIMIT %s", (vivier, PAGE))
    assert len(ref) == PAGE
    assert ligne_a_ligne[0] >= 30, ligne_a_ligne[0]

    lectures = {
        "historique": lambda: db.datastore_list_rows(
            vivier, offset=0, limit=PAGE, order_by="_id", order_dir="asc"),
        "mince": lambda: db.datastore_list_rows(
            vivier, offset=0, limit=PAGE, order_by="_id", order_dir="asc",
            filters=[{"field": "statut", "op": "ne", "value": "absent"}]),
        "curseur": lambda: db.datastore_list_rows_after(vivier, limit=PAGE),
        "page+stats": lambda: db.datastore_page_with_stats(
            vivier, offset=0, limit=PAGE, order_by="_id", order_dir="asc",
            filters=[{"field": "statut", "op": "ne", "value": "absent"}])[0],
    }
    for nom, lire in lectures.items():
        lire()  # connexion chaude : la mesure ne compte que la page
        with _attentes() as n:
            rows = lire()
        assert len(rows) == PAGE, nom
        # Seuil RELATIF : le nombre absolu d'attentes dépend de la machine (tampons
        # libpq, charge : 4-7 en local, 16 vu en CI), le rapport à la lecture ligne à
        # ligne du même jeu, mesurée dans ce test, non. Ancien comportement : rapport
        # ~1 ; correctif : ~1/10. On exige au plus un quart.
        assert n[0] * 4 <= ligne_a_ligne[0], (
            f"{nom} : {n[0]} attentes de socket pour une page de {PAGE} lignes "
            f"({ligne_a_ligne[0]} ligne à ligne ; il en faut au plus un quart) — chaque "
            f"attente rend le GIL, et à côté d'un thread qui calcule chacune coûte "
            f"jusqu'à l'intervalle de bascule (oto-backend#980)")
