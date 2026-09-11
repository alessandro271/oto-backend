"""Signaux #888/#890 — un `[[Titre]]` se résout parmi les projets que possède l'org.

Le fait mesuré le 11/09/2026 : aucun chemin de création d'org n'a jamais posé
d'ancre (`orgs.kb_project_id`) ; elle naissait paresseusement, par le tableau de
bord puis par `oto_kb`, retiré le 10/09. Une org créée depuis ne pouvait donc lier
aucune page d'un projet à une autre — une carte « base partagée + projets de
mouvement » n'était plus reproductible. Décision du même jour : la résolution se
fait par titre exact parmi les projets POSSÉDÉS par l'org ; un titre porté par
plusieurs d'entre eux est ambigu, et c'est dit ; la frontière de l'org n'est jamais
franchie.

Contre un vrai PostgreSQL (bootée par `init_db`) et par le chemin SERVI (`oto_doc`) :
les requêtes de portée sont du SQL qu'une doublure ne jugerait pas. L'org du banc
est créée par `org_store.create_org`, donc SANS ancre — le cas du signal. Chaque
test travaille sur ses propres titres.
"""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def monde(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_888_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    dbconn._pool = None
    try:
        from oto_mcp import db, org_store
        from oto_mcp.db import init_db
        init_db()
        sub, voisin = "u-888", "u-voisin"
        org = org_store.create_org("Neuve", created_by=sub)
        org_store.add_org_member(org, sub, "org_admin")
        org_store.set_active_org(sub, org)
        autre = org_store.create_org("Voisine", created_by=voisin)
        org_store.add_org_member(autre, voisin, "org_admin")
        # L'auteur est AUSSI membre de l'autre org : si la frontière ne tenait qu'à
        # ses droits, elle céderait ici. Elle doit tenir à la PAGE, pas à l'auteur.
        org_store.add_org_member(autre, sub, "org_member")
        yield {
            "sub": sub, "org": org,
            "base": int(db.create_project("org", str(org), "Base", created_by=sub)),
            "outbound": int(db.create_project("org", str(org), "Outbound", created_by=sub)),
            "motion": int(db.create_project("org", str(org), "Motion", created_by=sub)),
            "ailleurs": int(db.create_project("org", str(autre), "Ailleurs",
                                              created_by=voisin)),
        }
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _servi(m: dict, **args) -> dict:
    """UN appel d'`oto_doc` par le chemin servi (dispatcher + gates)."""
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.docs import core as D
    return D._doc(ResolvedCtx(sub=m["sub"], org_id=m["org"]), D.DocInput(**args))


def _titre(prefixe: str) -> str:
    return f"{prefixe} {uuid.uuid4().hex[:6]}"


def _cites(m: dict, doc_id: int) -> int:
    return _servi(m, op="backlinks", doc_id=doc_id)["count"]


def test_une_org_SANS_ancre_lie_une_page_d_un_autre_de_ses_projets(monde):
    """Le signal #888 mot pour mot : une page d'« Outbound » cite une page de la
    base partagée de l'org. Avant : lien mort. Maintenant : lié."""
    t = _titre("HubSpot Conventions")
    cible = _servi(monde, op="create", project_id=monde["base"], title=t)
    citante = _servi(monde, op="create", project_id=monde["outbound"], title=_titre("Brief"),
                     body_md=f"cf [[{t}]]")
    assert "citations_sans_cible" not in citante
    assert "citations_ambigues" not in citante
    assert _cites(monde, cible["id"]) == 1


def test_une_citation_ecrite_AVANT_sa_cible_se_lie_quand_elle_nait_ailleurs(monde):
    """La re-résolution suit la même portée : une souche écrite dans un projet se lie
    quand la page naît dans un AUTRE projet de l'org."""
    t = _titre("Tarifs")
    citante = _servi(monde, op="create", project_id=monde["outbound"], title=_titre("Relance"),
                     body_md=f"voir [[{t}]]")
    assert citante["citations_sans_cible"] == [t]
    cible = _servi(monde, op="create", project_id=monde["base"], title=t)
    assert _cites(monde, cible["id"]) == 1


def test_un_titre_porte_par_DEUX_projets_de_l_org_n_est_pas_devine(monde):
    """Décision du 11/09 : jamais choisir au hasard. Rien n'est lié, les deux
    candidats sont nommés, et aucune des deux pages ne se croit citée."""
    t = _titre("Charte")
    a = _servi(monde, op="create", project_id=monde["base"], title=t)
    b = _servi(monde, op="create", project_id=monde["motion"], title=t)
    citante = _servi(monde, op="create", project_id=monde["outbound"], title=_titre("Note"),
                     body_md=f"selon [[{t}]]")
    amb = citante["citations_ambigues"]
    assert [x["titre"] for x in amb] == [t]
    assert sorted(c["doc_id"] for c in amb[0]["candidats"]) == sorted([a["id"], b["id"]])
    assert "citations_sans_cible" not in citante
    assert _cites(monde, a["id"]) == 0 and _cites(monde, b["id"]) == 0


def test_le_projet_de_la_page_l_emporte_sans_ambiguite(monde):
    """Le titre existe ailleurs dans l'org ET dans le projet de la page : le plus
    proche l'emporte, ce n'est pas une ambiguïté."""
    t = _titre("Process")
    loin = _servi(monde, op="create", project_id=monde["base"], title=t)
    pres = _servi(monde, op="create", project_id=monde["outbound"], title=t)
    citante = _servi(monde, op="create", project_id=monde["outbound"], title=_titre("Page"),
                     body_md=f"[[{t}]]")
    assert "citations_ambigues" not in citante
    assert _cites(monde, pres["id"]) == 1 and _cites(monde, loin["id"]) == 0


def test_la_frontiere_de_l_org_n_est_jamais_franchie(monde):
    """La page vit dans un projet d'une AUTRE org dont l'auteur est pourtant membre :
    elle reste hors de portée. La portée tient à la page, jamais à l'auteur."""
    from oto_mcp import db
    t = _titre("Secret")
    ailleurs = db.create_doc(monde["ailleurs"], t, body_md="", created_by="u-voisin")
    citante = _servi(monde, op="create", project_id=monde["outbound"], title=_titre("Page"),
                     body_md=f"[[{t}]]")
    assert citante["citations_sans_cible"] == [t]
    assert _servi(monde, op="backlinks", doc_id=ailleurs)["count"] == 0
