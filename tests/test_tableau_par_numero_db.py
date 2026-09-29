"""`scripts/tableaux_par_numero.py` sur une vraie base : à blanc, puis appliqué.

Le script range sous leur NUMÉRO les tableaux que la base désigne encore par leur nom
(flottes, liens de projet, portées de jetons, `datastore="…"` des procédures). Ce banc
vérifie ce que le STOCKAGE porte après coup — et, surtout, qu'à blanc rien ne bouge et
qu'un nom qui ne résout pas reste tel quel.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[1]
ALICE = "usr_tableau_par_numero_alice"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "tableaux_par_numero", RACINE / "scripts/tableaux_par_numero.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store
    from oto_mcp.db._conn import _connect

    db.upsert_user(ALICE, email=f"{ALICE}@t.invalid", name=ALICE)
    org = org_store.create_org("Org numéro", created_by=ALICE)
    org_store.add_org_member(org, ALICE, "org_member")
    org_store.set_active_org(ALICE, org)
    vivier = db.create_datastore("org", str(org), "vivier")
    projet = db.create_project("org", str(org), "Prospection", created_by=ALICE)
    with _connect() as conn:
        conn.execute("INSERT INTO project_links (project_id, target_type, target_ref) "
                     "VALUES (%s, 'tableau', 'vivier')", (projet,))
    org_store.set_instruction(
        "org", org, "prospecter",
        'Réserve : data_claim_next(datastore="vivier"), puis '
        'data_rows(datastore="introuvable"). La prose qui cite vivier reste.',
        set_by=ALICE)
    jeton = db.create_api_token(ALICE, label="scout",
                                scopes={"namespaces": {"vivier": "read"}})
    return {"org": org, "vivier": vivier, "projet": projet, "jeton": jeton}


def _etat(monde) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        lien = conn.execute("SELECT target_ref FROM project_links WHERE project_id = %s",
                            (monde["projet"],)).fetchone()["target_ref"]
        corps = conn.execute("SELECT body_md FROM org_instructions WHERE slug = "
                             "'prospecter'").fetchone()["body_md"]
        portee = conn.execute("SELECT scopes FROM user_api_tokens WHERE sub = %s",
                              (ALICE,)).fetchone()["scopes"]
    return {"lien": lien, "corps": corps, "portee": portee}


def test_a_blanc_rien_ne_bouge_et_le_rapport_compte(monde, script, capsys, tmp_path):
    avant = _etat(monde)
    assert script.main(["x", "--seulement", "liens", "--seulement", "jetons",
                        "--seulement", "contenus", "--sauvegarde", str(tmp_path)]) == 0
    sortie = capsys.readouterr().out
    assert "À BLANC" in sortie
    assert _etat(monde) == avant
    assert not list(tmp_path.iterdir()), "à blanc, aucune sauvegarde n'est écrite"


def test_applique_range_sous_le_numero_et_garde_l_avant(monde, script, tmp_path):
    vivier = str(monde["vivier"])
    assert script.main(["x", "--appliquer", "--seulement", "liens", "--seulement",
                        "jetons", "--seulement", "contenus",
                        "--sauvegarde", str(tmp_path)]) == 0
    apres = _etat(monde)
    assert apres["lien"] == vivier
    assert apres["portee"] == {"namespaces": {vivier: "read"}}
    assert f"data_claim_next(datastore={vivier})" in apres["corps"]
    assert 'datastore="introuvable"' in apres["corps"], "un nom introuvable reste"
    assert "La prose qui cite vivier reste." in apres["corps"]
    sauvegarde = json.loads(next(tmp_path.iterdir()).read_text())
    assert {e["section"] for e in sauvegarde} == {"liens", "jetons", "contenus"}


def test_rejouer_ne_fait_plus_rien(monde, script, capsys, tmp_path):
    avant = _etat(monde)
    assert script.main(["x", "--appliquer", "--seulement", "liens", "--seulement",
                        "jetons", "--seulement", "contenus",
                        "--sauvegarde", str(tmp_path)]) == 0
    assert _etat(monde) == avant
    assert not list(tmp_path.iterdir())
