"""Agréger à travers les fiches d'une colonne-liste (oto#22, point a du 30/09).

`group_by: "contacts[].fonction"` : « la répartition des fonctions, tous contacts
confondus » — le cœur de l'usage, qui obligeait jusqu'ici à énumérer les rangs
(`contacts[0].fonction`, `contacts[1].fonction`…). Chaque item de chaque ligne retenue
est une OCCURRENCE : `count` compte les contacts, `count_rows` les fiches — le
vocabulaire de l'union, qu'on ne redéfinit pas.

Contre un VRAI PostgreSQL : ce qui compte est ce que la requête REND, et une partie
des lignes n'est pas encore une liste pendant une conversion (`jsonb_array_elements`
lèverait). Aucune assertion sur du texte SQL ne l'aurait montré.
"""
from __future__ import annotations

import json

import psycopg
import pytest


def _ddl() -> str:
    from oto_mcp.db import _schema
    src = _schema._SCHEMA
    i = src.index("CREATE TABLE IF NOT EXISTS datastore_rows")
    j = src.index("\n);", i) + 3
    return src[i:j].replace("REFERENCES user_datastores(id) ON DELETE CASCADE", "")


_LIGNES = [
    ("r1", {"secteur": "A", "contacts": [
        {"fonction": "DRH", "age": 40,
         "email": {"valeur": "rh@a.fr", "origine": "socle"}},
        {"fonction": "Dirigeant", "age": 50}]}),
    ("r2", {"secteur": "B", "contacts": [
        {"fonction": "DRH", "age": 30,
         "email": {"valeur": "rh@b.fr", "origine": "annuaire"}},
        {"fonction": "DRH", "age": "n/a"},
        {"fonction": "Commercial"}]}),
    ("r3", {"secteur": "A", "contacts": []}),
    ("r4", {"secteur": "B"}),
    # Pendant une conversion, une partie des lignes n'est PAS encore une liste.
    ("r5", {"secteur": "B", "contacts": "Dupont, Martin"}),
    ("r6", {"secteur": "A", "contacts": [
        {"age": 20, "email": {"valeur": "x@c.fr", "origine": "socle"}}]}),
]


@pytest.fixture()
def pg(pg_module_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_module_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_module_dsn)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS datastore_rows")
        c.execute(_ddl())
        for rid, data in _LIGNES:
            c.execute("INSERT INTO datastore_rows (ns_id,row_id,data) "
                      "VALUES (1,%s,%s::jsonb)", (rid, json.dumps(data)))
        yield c
        c.execute("DROP TABLE IF EXISTS datastore_rows")


def _agg(**kw) -> list:
    from oto_mcp import db
    return db.datastore_aggregate(1, **kw)


def _par(res: list, cle: str) -> dict:
    return {r[cle]: {k: v for k, v in r.items() if k != cle} for r in res}


_COMPTES = [{"op": "count"}, {"op": "count_rows"}]


# --- la répartition tous contacts confondus ------------------------------------------

def test_la_repartition_compte_les_occurrences_et_les_fiches(pg):
    """LA question : trois DRH sur deux fiches. `count` et `count_rows` y divergent,
    et c'est exactement ce qu'ils existent pour distinguer."""
    res = _par(_agg(group_by="contacts[].fonction", metrics=_COMPTES),
               "contacts[].fonction")
    assert res == {
        "DRH": {"count": 3, "count_rows": 2},
        "Dirigeant": {"count": 1, "count_rows": 1},
        "Commercial": {"count": 1, "count_rows": 1},
        # Un contact sans fonction reste un contact : il forme le groupe vide, comme
        # une ligne sans valeur sous un `group_by` ordinaire.
        None: {"count": 1, "count_rows": 1},
    }


def test_trie_par_la_premiere_metrique(pg):
    res = _agg(group_by="contacts[].fonction", metrics=_COMPTES)
    assert res[0]["contacts[].fonction"] == "DRH"


def test_une_liste_vide_absente_ou_non_convertie_ne_fabrique_rien(pg):
    """Aucune occurrence pour `[]`, pour une colonne absente, ni pour une valeur qui
    n'est pas encore une liste — et surtout pas d'exception sur cette dernière."""
    res = _agg(group_by="contacts[].fonction", metrics=[{"op": "count"}])
    assert sum(r["count"] for r in res) == 6


def test_les_filtres_restreignent_les_lignes_avant_le_deroulement(pg):
    res = _par(_agg(group_by="contacts[].fonction", metrics=_COMPTES,
                    filters=[{"field": "secteur", "op": "eq", "value": "A"}]),
               "contacts[].fonction")
    assert res == {"DRH": {"count": 1, "count_rows": 1},
                   "Dirigeant": {"count": 1, "count_rows": 1},
                   None: {"count": 1, "count_rows": 1}}


def test_un_filtre_d_existence_garde_la_fiche_entiere(pg):
    """« Les fiches dont UN contact est commercial » : la fiche r2 entre, et TOUS ses
    contacts sont comptés — le filtre choisit des lignes, pas des items."""
    res = _par(_agg(group_by="contacts[].fonction", metrics=_COMPTES,
                    filters=[{"field": "contacts[].fonction", "op": "eq",
                              "value": "Commercial"}]),
               "contacts[].fonction")
    assert res == {"DRH": {"count": 2, "count_rows": 1},
                   "Commercial": {"count": 1, "count_rows": 1}}


def test_la_couche_d_un_attribut_se_compte(pg):
    """Le besoin daté du 14/08 : « combien d'adresses viennent de tel type de
    source ». La traçabilité par champ n'est complète que si elle se COMPTE."""
    res = _par(_agg(group_by="contacts[].email.origine", metrics=_COMPTES),
               "contacts[].email.origine")
    assert res["socle"] == {"count": 2, "count_rows": 2}
    assert res["annuaire"] == {"count": 1, "count_rows": 1}


def test_une_metrique_conditionnelle_reste_au_grain_de_l_occurrence(pg):
    res = _par(_agg(group_by="contacts[].fonction", metrics=[
        {"op": "count"},
        {"op": "count", "label": "secteur_b",
         "where": [{"field": "secteur", "op": "eq", "value": "B"}]}]),
        "contacts[].fonction")
    assert res["DRH"] == {"count": 3, "secteur_b": 2}


# --- les métriques numériques sur tous les items -------------------------------------

def test_les_metriques_d_un_attribut_portent_sur_tous_les_items(pg):
    """Sans groupe : 40, 50, 30, 20 (« n/a » n'est pas un nombre, il est ignoré comme
    au premier niveau). `count` sans champ compte toujours les LIGNES."""
    [r] = _agg(metrics=[{"op": "count"},
                        {"op": "count", "field": "contacts[].age"},
                        {"op": "sum", "field": "contacts[].age"},
                        {"op": "avg", "field": "contacts[].age"},
                        {"op": "min", "field": "contacts[].age"},
                        {"op": "max", "field": "contacts[].age"}])
    assert r == {"count": 6, "count_contacts[].age": 5,
                 "sum_contacts[].age": 140, "avg_contacts[].age": 35,
                 "min_contacts[].age": 20, "max_contacts[].age": 50}


def test_la_moyenne_pese_chaque_item_pas_chaque_fiche(pg):
    """Groupé par une colonne de la LIGNE : secteur A porte 40, 50 (r1) et 20 (r6).
    La moyenne des items vaut 36,67 ; une moyenne des moyennes par fiche vaudrait 32,5
    — un chiffre plausible et faux."""
    res = _par(_agg(group_by="secteur", metrics=[
        {"op": "count"}, {"op": "avg", "field": "contacts[].age"},
        {"op": "count", "field": "contacts[].age"}]), "secteur")
    assert res["A"]["count"] == 3          # des LIGNES : rien n'est déroulé
    assert res["A"]["avg_contacts[].age"] == pytest.approx(110 / 3)
    assert res["A"]["count_contacts[].age"] == 3
    assert res["B"]["avg_contacts[].age"] == 30
    assert res["B"]["count_contacts[].age"] == 2


def test_sous_la_liste_deroulee_l_attribut_se_lit_sur_l_item_courant(pg):
    res = _par(_agg(group_by="contacts[].fonction", metrics=[
        {"op": "avg", "field": "contacts[].age"},
        {"op": "max", "field": "contacts[].age"}]), "contacts[].fonction")
    assert res["DRH"] == {"avg_contacts[].age": 35, "max_contacts[].age": 40}
    assert res["Dirigeant"] == {"avg_contacts[].age": 50, "max_contacts[].age": 50}
    assert res[None] == {"avg_contacts[].age": 20, "max_contacts[].age": 20}


# --- ce qui reste refusé -------------------------------------------------------------

def test_une_autre_liste_sous_une_liste_deroulee_est_refusee(pg):
    with pytest.raises(ValueError) as e:
        _agg(group_by="contacts[].fonction",
             metrics=[{"op": "sum", "field": "sites[].surface"}])
    assert "contacts" in str(e.value) and "sites" in str(e.value)


def test_une_liste_ne_se_met_pas_en_commun_avec_d_autres_colonnes(pg):
    with pytest.raises(ValueError) as e:
        _agg(group_by=["contacts[].fonction", "dirigeant_fonction"])
    assert "group_by" in str(e.value)


def test_le_tri_sur_tous_les_items_reste_refuse(pg):
    from oto_mcp import db
    with pytest.raises(ValueError) as e:
        db.datastore_list_rows(1, order_by="contacts[].fonction", limit=10)
    assert "contacts[0].fonction" in str(e.value)


def test_la_surface_du_store_sert_la_repartition(pg, monkeypatch):
    """Le chemin de `data_aggregate` et de `GET …/aggregate` : le store ne réécrit
    pas le `group_by`, et la clé du groupe est le chemin tel que demandé."""
    from oto_mcp.datastore.core import DatastorePg
    s = DatastorePg("u-1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 1)
    monkeypatch.setattr(s, "_schema_of", lambda ns_id: None)
    res = s.aggregate("t", group_by="contacts[].fonction",
                      metrics=[{"op": "count"}], filter={"secteur": "B"})
    assert res[0] == {"contacts[].fonction": "DRH", "count": 2}
