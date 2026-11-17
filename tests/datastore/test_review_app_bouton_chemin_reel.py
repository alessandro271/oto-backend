"""Le bouton de `data_review_app`, par le VRAI chemin : le serveur servi, une base réelle.

Depuis fastmcp 3.4.8, l'appel d'un outil d'app par son nom haché
(`<hash>_data_review_decide`) suit les mêmes règles que l'appel par son nom affiché :
masquage de la session, état activé, autorisation. Ce banc prouve, sur le catalogue que
le serveur construit (`_build_mcp`, toute la chaîne de middlewares), une vraie session
(client fastmcp en mémoire) et la base réelle, que :

1. le bouton MARCHE pour un utilisateur qui a le droit sur la table : l'outil haché que
   la carte adresse s'appelle, et la décision est ÉCRITE en base ;
2. un utilisateur sans droit sur la table, qui rejoue les arguments du bouton, est
   refusé — rien n'est écrit ;
3. l'outil masqué pour la session ne s'appelle plus, même par son nom haché — rien
   n'est écrit.

L'identité arrive comme le transport HTTP la pose après vérification du jeton
(`auth_context_var`) : c'est le seul élément fourni par le banc.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("prefab_ui")

STATUT = "statut"
SCHEMA = {
    "key": "contact",
    "fields": [
        {"key": "contact", "type": "text", "label": "Prospect", "display": "title"},
        {"key": STATUT, "role": "status", "type": "enum", "label": "Statut",
         "options": ["to_review", "launched", "skipped"]},
    ],
}


def _nodes(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _nodes(v)


def _boutons(result) -> list[dict]:
    return [n for n in _nodes(result.structured_content) if n.get("action") == "toolCall"]


@pytest.fixture(scope="module")
def serveur(live):
    """Le catalogue tel que le serveur le construit — pas d'instance de laboratoire."""
    from oto_mcp.server import _build_mcp
    return _build_mcp("noauth")


@pytest.fixture
def monde(live):
    """Une org et son membre ; une table de l'org à deux lignes à revoir ; un intrus,
    utilisateur réel, hors de l'org."""
    from oto_mcp import db, org_store
    from oto_mcp.datastore.core import make_store
    u = uuid.uuid4().hex[:8]
    membre, intrus = f"membre_{u}", f"intrus_{u}"
    for sub in (membre, intrus):
        db.upsert_user(sub, email=f"{sub}@example.test")
    org = int(org_store.create_org(f"o_{u}"))
    org_store.add_org_member(org, membre)
    org_store.set_active_org(membre, org)
    ns = str(db.create_datastore("org", str(org), f"revue_{u}"))
    store = make_store(membre)
    store.set_schema(ns, SCHEMA)
    store.write_rows(ns, [{"contact": "Jane Doe", STATUT: "to_review"},
                          {"contact": "John Roe", STATUT: "to_review"}], key="contact")
    return {"membre": membre, "intrus": intrus, "org": org, "ns": ns}


def _statuts(m) -> dict:
    from oto_mcp.datastore.core import make_store
    rows = make_store(m["membre"]).list_rows(m["ns"], limit=10)
    return {r["contact"]: r[STATUT] for r in rows}


def _en_session(serveur, sub, corps):
    """Une vraie session MCP au nom de `sub` : l'identité posée là où le transport HTTP
    la pose après vérification du jeton."""
    from fastmcp import Client
    from fastmcp.server.auth.auth import AccessToken
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    jeton = AccessToken(token=f"t-{sub}", client_id="banc", scopes=[], subject=sub,
                        claims={"sub": sub})

    async def _go():
        marque = auth_context_var.set(AuthenticatedUser(jeton))
        try:
            async with Client(serveur) as c:
                return await corps(c)
        finally:
            auth_context_var.reset(marque)
    return asyncio.run(_go())


def _ouvrir(m) -> dict:
    return {"datastore": m["ns"], "pending": "to_review", "approve": "launched",
            "reject": "skipped"}


def _bouton_lance(serveur, m) -> dict:
    """Le bouton « launched » de la carte servie au membre."""
    async def corps(c):
        return _boutons(await c.call_tool("data_review_app", _ouvrir(m)))
    boutons = _en_session(serveur, m["membre"], corps)
    assert len(boutons) == 2, "la carte doit offrir ses deux boutons"
    return next(b for b in boutons if b["arguments"]["value"] == "launched")


def test_le_bouton_ecrit_la_decision_pour_le_membre(serveur, monde):
    bouton = _bouton_lance(serveur, monde)
    assert bouton["tool"].endswith("_data_review_decide") and bouton["tool"] != "data_review_decide"

    async def corps(c):
        return await c.call_tool(bouton["tool"], bouton["arguments"])
    res = _en_session(serveur, monde["membre"], corps)
    assert not res.is_error
    assert _statuts(monde) == {"Jane Doe": "launched", "John Roe": "to_review"}
    # la carte suivante adresse la ligne suivante, par le même outil haché
    suivants = _boutons(res)
    assert suivants and {b["tool"] for b in suivants} == {bouton["tool"]}


@pytest.mark.parametrize("axes", ["figes", "retires"])
def test_le_bouton_rejoue_par_un_utilisateur_sans_droit_est_refuse(serveur, monde, axes):
    """L'intrus rejoue les arguments du bouton : avec le contexte figé par la carte,
    la garde de l'axe le refuse ; sans lui, c'est la table qui lui reste fermée."""
    bouton = _bouton_lance(serveur, monde)
    args = dict(bouton["arguments"])
    if axes == "retires":
        args.update(project=None, org=None)

    async def corps(c):
        return await c.call_tool(bouton["tool"], args, raise_on_error=False)
    res = _en_session(serveur, monde["intrus"], corps)
    assert res.is_error
    assert _statuts(monde) == {"Jane Doe": "to_review", "John Roe": "to_review"}


def test_l_outil_masque_ne_s_appelle_plus_par_son_nom_hache(serveur, monde, monkeypatch):
    """Une règle de visibilité qui masque `data_review_decide` mord aussi sur son nom
    haché : le membre, qui a le droit, est refusé et rien n'est écrit."""
    bouton = _bouton_lance(serveur, monde)
    monkeypatch.setattr(serveur, "_transforms", list(serveur._transforms))
    serveur.disable(names={"data_review_decide"})

    async def corps(c):
        return await c.call_tool(bouton["tool"], bouton["arguments"], raise_on_error=False)
    res = _en_session(serveur, monde["membre"], corps)
    assert res.is_error
    assert _statuts(monde) == {"Jane Doe": "to_review", "John Roe": "to_review"}
