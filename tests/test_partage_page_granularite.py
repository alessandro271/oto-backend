"""Partager UNE page partage CETTE page — signal #666, 02/09/2026.

Le rapport : « partager un document partage en réalité tout le PROJET qui le
contient », avec pour conséquence l'abandon du geste (contournement : recopier le
corps dans un e-mail) alors qu'il fait exactement ce qu'on lui demandait.

Ce que le code fait vraiment, et que rien ne testait : `set_public` pose le jeton
(chiffré, et son empreinte) SUR LA LIGNE de la page, et la lecture publique
(`get_doc_by_public_token`, servie par `/api/public/docs/{token}` et `/p/d/{token}`)
sélectionne PAR CE TOKEN — jamais par projet. Le voisinage n'est donc pas joignable.

Ce qui a manqué n'était pas le mécanisme, c'était la PHRASE : la description servie
disait « shareable public read-only link » sans dire de QUOI. Elle le dit désormais,
et cet invariant-ci l'empêche de devenir faux en silence — un partage qui déborderait
sur la fratrie ou le projet serait une fuite, pas une régression de confort.

⚠️ Base réelle : un partage se juge sur le SQL exécuté, pas sur un double de seam
qui rendrait ce qu'on lui a soufflé.
"""
from __future__ import annotations

import os
import uuid

import pytest

# Publier scelle le jeton du lien (`db/jetons_de_lien.py`) : il faut la clé maître.
pytestmark = pytest.mark.usefixtures("cle_maitre")


@pytest.fixture(scope="module")
def arbre(live) -> dict:
    """Le cas du signal, en miniature : un projet qui porte les pages de PLUSIEURS
    dossiers, dont une seule doit partir chez un tiers — et cette page a elle-même
    une sous-page qui, elle, ne doit pas partir."""
    from oto_mcp import db
    pid = db.create_project("user", "sub-666", "Clients — missions en cours")
    partagee = db.create_doc(pid, "Chiffrage — mission A", body_md="le chiffrage")
    return {
        "projet": pid,
        "partagee": partagee,
        "voisine": db.create_doc(pid, "Mission B — confidentiel",
                                 body_md="une autre mission"),
        "enfant": db.create_doc(pid, "Annexe de la mission A",
                                parent_id=partagee, body_md="l'annexe"),
    }


def test_le_token_ouvre_la_page_partagee(arbre):
    from oto_mcp import db
    token = db.set_doc_public(arbre["partagee"], True)
    assert token
    lu = db.get_doc_by_public_token(token)
    assert lu["title"] == "Chiffrage — mission A"
    assert lu["body_md"] == "le chiffrage"


def test_le_token_n_ouvre_QUE_cette_page(arbre):
    """L'invariant du signal, dans les deux directions qui font peur : la page
    VOISINE (un autre dossier client, dans le même projet) et la SOUS-PAGE de la page
    partagée restent privées. Aucune des deux n'a de token, et le token existant
    n'en désigne qu'une."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    token = db.set_doc_public(arbre["partagee"], True)
    with _connect() as conn:
        porteuses = conn.execute(
            "SELECT id FROM docs WHERE project_id = %s AND public_token_hash IS NOT NULL",
            (arbre["projet"],)).fetchall()
    assert [r["id"] for r in porteuses] == [arbre["partagee"]]
    # …et le contenu servi ne cite ni l'une ni l'autre.
    lu = db.get_doc_by_public_token(token)
    assert "une autre mission" not in lu["body_md"]
    assert "l'annexe" not in lu["body_md"]


def test_le_jeton_est_range_chiffre_jamais_en_clair(arbre):
    """L'enveloppe (clé maître, AAD de la ligne) et l'empreinte, jamais le jeton : la
    ligne ne le porte nulle part, et le lien reste le même tant que la page est publique."""
    from oto_mcp import crypto, db
    from oto_mcp.db import jetons_de_lien
    from oto_mcp.db._conn import _connect
    token = db.set_doc_public(arbre["partagee"], True)
    with _connect() as conn:
        row = conn.execute("SELECT * FROM docs WHERE id = %s",
                           (arbre["partagee"],)).fetchone()
    assert row["public_token"] is None
    assert token not in str(list(row.values()))
    assert row["public_token_hash"] == jetons_de_lien.empreinte(token)
    assert crypto.decrypt(row["public_token_enc"],
                          f"docs:{arbre['partagee']}:public_token") == token
    with pytest.raises(crypto.EnveloppeIndechiffrable):
        crypto.decrypt(row["public_token_enc"], f"docs:{arbre['voisine']}:public_token")
    assert db.get_doc_by_id(arbre["partagee"])["public_token"] == token
    assert db.set_doc_public(arbre["partagee"], True) == token, "le lien ne change pas"


def test_un_jeton_inconnu_n_ouvre_rien(arbre):
    from oto_mcp import db
    db.set_doc_public(arbre["partagee"], True)
    assert db.get_doc_by_public_token("jeton-inconnu") is None


def test_retirer_le_partage_ferme_la_lecture(arbre):
    from oto_mcp import db
    token = db.set_doc_public(arbre["partagee"], True)
    assert db.get_doc_by_public_token(token) is not None
    assert db.set_doc_public(arbre["partagee"], False) is None
    assert db.get_doc_by_public_token(token) is None


def test_la_description_servie_dit_la_PORTEE_du_lien():
    """La cause du signal : la description ne disait pas de quoi le lien ouvre la
    porte, et un lecteur prudent a supposé le pire (« tout le projet »), puis a
    renoncé au geste. Le mécanisme n'a jamais eu besoin d'être corrigé — sa phrase,
    si."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.mcp == "oto_doc")
    d = cap.description
    assert "THIS PAGE ALONE" in d
    assert "not the project" in d
    assert "sub-pages" in d
