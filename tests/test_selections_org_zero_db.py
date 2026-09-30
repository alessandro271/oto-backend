"""`scripts/selections_org_zero.py` sur une vraie base (#959) : à blanc, puis appliqué.

Les lignes de `user_selected_connectors` sous l'ancienne sentinelle `org_id=0` ne sont
rendues par aucune lecture. Ce banc vérifie ce que le STOCKAGE porte après coup : à
blanc rien ne bouge ; appliqué, l'org réelle fait foi (un doublon ou un RETRAIT du
membre n'est jamais écrasé), un connecteur disparu part, un reste suit `--restes`, un
compte sans org n'est touché qu'avec `--purger-orphelins` — et plus rien ne reste
sous `0`.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[1]
DISPARU = "connecteur_disparu_959"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "selections_org_zero", RACINE / "scripts/selections_org_zero.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _monde(prefixe: str) -> dict:
    """Trois comptes : A (org réelle AVEC sélection), B (org réelle SANS sélection),
    O (aucune org). Chacun porte des lignes sous `0`."""
    from oto_mcp import db, org_store
    from oto_mcp.db._conn import _connect

    a, b, o = (f"usr_959_{prefixe}_{x}" for x in "abo")
    for sub in (a, b, o):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    org_a = org_store.create_org(f"Org A {prefixe}", created_by=a)
    org_store.add_org_member(org_a, a, "org_member")
    org_store.set_active_org(a, org_a)
    org_b = org_store.create_org(f"Org B {prefixe}", created_by=b)
    org_store.add_org_member(org_b, b, "org_member")
    org_store.set_active_org(b, org_b)
    with _connect() as conn:
        conn.execute("DELETE FROM org_members WHERE sub = %s", (o,))
        conn.execute("DELETE FROM user_selected_connectors WHERE sub = ANY(%s)",
                     ([a, b, o],))
        ins = ("INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
               "VALUES (%s, %s, %s, %s)")
        # A : sous son org réelle, serper en PAUSE (son geste) ; retrait de hunter.
        conn.execute(ins, (a, org_a, "serper", "paused"))
        conn.execute("INSERT INTO connector_selection_removed (sub, org_id, connector) "
                     "VALUES (%s, %s, 'hunter')", (a, org_a))
        for c in ("serper", "hunter", "apollo", DISPARU):
            conn.execute(ins, (a, 0, c, "active"))
        # B : rien sous son org réelle, un reste sous 0.
        conn.execute(ins, (b, 0, "apollo", "paused"))
        # O : aucune org.
        conn.execute(ins, (o, 0, "serper", "active"))
    return {"a": a, "b": b, "o": o, "org_a": org_a, "org_b": org_b}


def _lignes(*subs) -> set:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, org_id, connector, state FROM user_selected_connectors "
            "WHERE sub = ANY(%s)", (list(subs),)).fetchall()
    return {(r["sub"], r["org_id"], r["connector"], r["state"]) for r in rows}


def test_classement_des_quatre_cas(live, script):
    m = _monde("classe")
    from oto_mcp.db._conn import _connect
    with _connect() as conn, conn.cursor() as cur:
        sorts = {(l["sub"], l["connector"]): l["sort"] for l in script.classer(cur)}
    assert sorts[(m["a"], DISPARU)] == "disparus"
    assert sorts[(m["a"], "serper")] == "doublons"      # sélectionné sous l'org réelle
    assert sorts[(m["a"], "hunter")] == "doublons"      # RETIRÉ sous l'org réelle
    assert sorts[(m["a"], "apollo")] == "restes"
    assert sorts[(m["b"], "apollo")] == "restes"
    assert sorts[(m["o"], "serper")] == "orphelins"


def test_a_blanc_rien_ne_bouge(live, script, tmp_path):
    m = _monde("blanc")
    avant = _lignes(m["a"], m["b"], m["o"])
    assert script.main(["x", "--sauvegarde", str(tmp_path)]) == 0
    assert _lignes(m["a"], m["b"], m["o"]) == avant
    assert not list(tmp_path.iterdir())


def test_appliquer_sans_choix_des_restes_refuse(live, script, tmp_path):
    m = _monde("refus")
    avant = _lignes(m["a"], m["b"], m["o"])
    assert script.main(["x", "--appliquer", "--sauvegarde", str(tmp_path)]) == 2
    assert _lignes(m["a"], m["b"], m["o"]) == avant


def test_repointer_garde_l_org_reelle_et_ne_laisse_rien_sous_zero(live, script, tmp_path):
    m = _monde("repointe")
    assert script.main(["x", "--appliquer", "--restes", "repointer", "--purger-orphelins",
                        "--sauvegarde", str(tmp_path)]) == 0
    apres = _lignes(m["a"], m["b"], m["o"])
    assert not [l for l in apres if l[1] == 0]
    assert apres == {
        (m["a"], m["org_a"], "serper", "paused"),    # son état réel, pas `active`
        (m["a"], m["org_a"], "apollo", "active"),    # le reste, repointé, état gardé
        (m["b"], m["org_b"], "apollo", "paused"),
    }                                                # hunter : son retrait tient
    assert list(tmp_path.glob("selections-org-zero-*.json"))


def test_purger_les_restes_et_garder_les_orphelins(live, script, tmp_path):
    m = _monde("purge")
    assert script.main(["x", "--appliquer", "--restes", "purger",
                        "--sauvegarde", str(tmp_path)]) == 0
    assert _lignes(m["a"], m["b"], m["o"]) == {
        (m["a"], m["org_a"], "serper", "paused"),
        (m["o"], 0, "serper", "active"),             # sans --purger-orphelins : listé
    }
