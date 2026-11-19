"""`oto-mcp maintenance jetons-de-lien` sur une vraie base : sceller les jetons de lien
encore rangés en clair.

Une base d'avant porte le jeton du lien public d'une page (`docs.public_token`) et celui
d'une procédure partagée (`process_shares.token`) en clair, plus deux copies (la fiche
d'un compte né d'un partage, un nœud recopié). Ce banc vérifie ce que le STOCKAGE porte :
à blanc rien ne bouge ; `--sans-effacer` scelle et garde le clair ; `--appliquer` scelle,
vide le clair et les copies ; les liens d'avant répondent ensuite, sous le même jeton, par
le code qui ne lit que l'enveloppe ; rejouée, la commande ne fait rien ; un écart n'est
jamais touché ; aucun jeton n'est imprimé.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.usefixtures("cle_maitre")

LIEN_PAGE = "lien-page-davant-0000000"
LIEN_PROC = "lien-proc-davant-00000"


def _commande(*args) -> int:
    from oto_mcp import maintenance
    return maintenance.main(["jetons-de-lien", *args])


def _un(sql, params=None):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(sql, params).fetchone()


def _sql(sql, params=None):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


@pytest.fixture
def avant(live):
    """Des liens émis par le code d'avant : jetons en clair, ni enveloppe ni empreinte."""
    from oto_mcp import db, org_store
    u = uuid.uuid4().hex[:8]
    sub, lecteur = f"u_{u}", f"lecteur_{u}"
    db.upsert_user(sub)
    db.upsert_user(lecteur)
    org = org_store.create_org(f"o_{u}", created_by=sub)
    org_store.add_org_member(org, sub, "org_admin")
    org_store.set_instruction("org", org, f"proc-{u}", "le corps", set_by=sub)
    instr = org_store.get_instruction("org", org, f"proc-{u}")
    projet = db.create_project("org", str(org), "Partage", created_by=sub)
    page = db.create_doc(projet, "Page publique", body_md="contenu", created_by=sub)
    lien_page, lien_proc = f"{LIEN_PAGE}-{u}", f"{LIEN_PROC}-{u}"
    _sql("UPDATE docs SET public_token = %s WHERE id = %s", (lien_page, page))
    partage = _un("INSERT INTO process_shares (instruction_id, org_id, token, created_by) "
                  "VALUES (%s, %s, %s, %s) RETURNING id",
                  (instr["id"], org, lien_proc, sub))["id"]
    _sql("INSERT INTO user_account_profile (sub, profile) VALUES (%s, %s) "
         "ON CONFLICT (sub) DO UPDATE SET profile = EXCLUDED.profile",
         (lecteur, json.dumps({"acquired_via": {"kind": "process_share",
                                                "token": lien_proc, "at": "t"}})))
    noeud = f"n_{u}"
    _sql("INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
         "VALUES (%s, 'page', 'org', %s, %s)",
         (noeud, str(org), json.dumps({"title": "x", "public_token": lien_page})))
    yield {"page": page, "partage": partage, "lecteur": lecteur, "noeud": noeud,
           "lien_page": lien_page, "lien_proc": lien_proc}
    _sql("DELETE FROM user_account_profile WHERE sub = %s", (lecteur,))
    _sql("DELETE FROM nodes WHERE public_id = %s", (noeud,))
    _sql("DELETE FROM process_shares WHERE id = %s", (partage,))
    _sql("DELETE FROM projects WHERE id = %s", (projet,))


def _clairs(m) -> tuple:
    return (_un("SELECT public_token FROM docs WHERE id = %s", (m["page"],))["public_token"],
            _un("SELECT token FROM process_shares WHERE id = %s", (m["partage"],))["token"])


def _resolus(m) -> tuple:
    from oto_mcp import db
    from oto_mcp.db import partages_procedure as db_partages
    page = db.get_doc_by_public_token(m["lien_page"])
    partage = db_partages.par_jeton(m["lien_proc"])
    return (page["title"] if page else None, partage["id"] if partage else None)


def test_a_blanc_rien_ne_bouge_et_rien_ne_fuit(avant, capsys):
    m = avant
    assert _commande() == 3
    sortie = capsys.readouterr().out
    assert "À BLANC" in sortie and f"docs #{m['page']} : à sceller" in sortie
    assert m["lien_page"] not in sortie and m["lien_proc"] not in sortie
    assert _clairs(m) == (m["lien_page"], m["lien_proc"])
    assert _resolus(m) == (None, None), "le code neuf ne lit pas le clair"


def test_sans_effacer_scelle_et_garde_le_clair(avant, capsys):
    from oto_mcp import db
    m = avant
    assert _commande("--appliquer", "--sans-effacer") == 0
    assert m["lien_page"] not in capsys.readouterr().out
    assert _clairs(m) == (m["lien_page"], m["lien_proc"])
    assert _resolus(m) == ("Page publique", m["partage"])
    assert db.get_doc_by_id(m["page"])["public_token"] == m["lien_page"]


def test_appliquer_scelle_vide_le_clair_et_les_copies(avant, capsys):
    from oto_mcp import db
    from oto_mcp.db import partages_procedure as db_partages
    m = avant
    assert _commande("--appliquer") == 0
    sortie = capsys.readouterr().out
    assert m["lien_page"] not in sortie and m["lien_proc"] not in sortie
    assert _clairs(m) == (None, None)
    # Les liens d'avant répondent, sous le même jeton, et se réaffichent.
    assert _resolus(m) == ("Page publique", m["partage"])
    assert db.get_doc_by_id(m["page"])["public_token"] == m["lien_page"]
    assert db.set_doc_public(m["page"], True) == m["lien_page"]
    assert db_partages.actif(
        db_partages.par_jeton(m["lien_proc"])["instruction_id"])["token"] == m["lien_proc"]
    via = _un("SELECT profile FROM user_account_profile WHERE sub = %s",
              (m["lecteur"],))["profile"]["acquired_via"]
    assert via == {"kind": "process_share", "share_id": m["partage"], "at": "t"}
    assert _un("SELECT props FROM nodes WHERE public_id = %s",
               (m["noeud"],))["props"] == {"title": "x"}
    # Rejouée : rien à faire.
    assert _commande("--appliquer") == 0
    assert _commande() == 0
    assert "aucun jeton de lien en clair" in capsys.readouterr().out


def test_un_ecart_n_est_jamais_touche(avant, capsys):
    from oto_mcp.db import jetons_de_lien
    m = avant
    autre = jetons_de_lien.sceller("un-autre-jeton", jetons_de_lien.aad_page(m["page"]))
    _sql("UPDATE docs SET public_token_enc = %s WHERE id = %s", (autre, m["page"]))
    assert _commande("--appliquer") == 1
    assert "écart" in capsys.readouterr().out
    assert _clairs(m)[0] == m["lien_page"], "la ligne en écart garde son clair"
    assert _un("SELECT public_token_enc FROM docs WHERE id = %s",
               (m["page"],))["public_token_enc"] == autre


def test_sans_cle_maitre_refus(avant, monkeypatch, capsys):
    m = avant
    monkeypatch.delenv("OTO_MCP_MASTER_KEY")
    assert _commande("--appliquer") == 2
    assert "OTO_MCP_MASTER_KEY" in capsys.readouterr().err
    assert _clairs(m) == (m["lien_page"], m["lien_proc"])
