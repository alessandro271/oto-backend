"""« Partagés avec moi » (`GET /api/me/datastores/shared`) — sur une VRAIE base.

La garantie qui compte est une garantie de CONFIDENTIALITÉ : elle se prouve sur le SQL
réel, pas sur un double. Un partage à une personne n'est visible QUE de son
destinataire — jamais d'un tiers de la même org, jamais par un droit d'org ou d'équipe.
C'est l'incident du 30/06 (des ressources d'une autre org visibles dans une vue d'org)
qu'on s'interdit de rejouer (arbitrage d'Alexis, otomata-tech/oto#160, 10/09/2026).

La distribution :
- A possède `t-perso` (personnel) et le partage à B en LECTURE — et s'y est aussi posé
  un droit à lui-même (un tableau possédé n'est pas « partagé avec moi ») ;
- l'org X possède `t-org`, que A partage AUSSI nominativement à B, membre de X ;
- A partage `t-a-l-org` à l'org X et `t-a-l-equipe` à une équipe de X : des droits
  d'AUDIENCE, pas des partages à une personne ;
- C est membre de X et de cette équipe, sans aucun partage nominatif ;
- Y est une autre org de B, qui ne possède rien.

L'org active est posée par appel (`access.current_org`, le seam unique) ; les
appartenances, elles, sont de vraies lignes.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, db, group_store, org_store, ownership
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.datastore import partages_recus as P
from oto_mcp.datastore.core import make_store

A, B, C = "u-partage-a", "u-partage-b", "u-partage-c"
DS = ownership.TYPE_RESSOURCE_DATASTORE


@pytest.fixture(scope="module")
def monde(live):
    for sub, nom in ((A, "Alice Proprio"), (B, "Bruno Dest"), (C, "Chloé Tiers")):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=nom)
    x = org_store.create_org("Org X", created_by=A)
    y = org_store.create_org("Org Y", created_by=B)
    org_store.add_org_member(x, B)
    org_store.add_org_member(x, C)
    org_store.add_org_member(y, B)
    equipe = group_store.create_group(x, "Équipe X", created_by=A)
    group_store.add_group_member(equipe, C)
    t = {nom: db.create_datastore(ot, oid, nom) for nom, ot, oid in (
        ("t-perso", "user", A), ("t-org", "org", str(x)),
        ("t-a-l-org", "user", A), ("t-a-l-equipe", "user", A))}
    ownership.grant(DS, str(t["t-perso"]), "user", B, "read", granted_by=A)
    ownership.grant(DS, str(t["t-perso"]), "user", A, "write", granted_by=A)
    ownership.grant(DS, str(t["t-org"]), "user", B, "write", granted_by=A)
    ownership.grant(DS, str(t["t-a-l-org"]), "org", str(x), "write", granted_by=A)
    ownership.grant(DS, str(t["t-a-l-equipe"]), "group", str(equipe), "write", granted_by=A)
    return {"x": x, "y": y, **t}


def _recus(monkeypatch, sub, org) -> dict:
    """La réponse de la capacité, pour `sub` naviguant dans `org` (None = aucune org)."""
    monkeypatch.setattr(access, "current_org", lambda s: org)
    out = P._shared_with_me(ResolvedCtx(sub=sub, org_id=org), P.SharedWithMeInput())
    # Ce que la face REST SERT : la forme déclarée (`Output`) appliquée à la réponse —
    # c'est elle qui pose `ns_id` et renomme `schema`, pas le handler.
    servi = P.SharedWithMe(**out).model_dump(by_alias=True)
    return {int(e["id"]): e for e in servi["datastores"]}


def _liste_de_l_org(monkeypatch, sub, org) -> set:
    monkeypatch.setattr(access, "current_org", lambda s: org)
    return {int(e["id"]) for e in make_store(sub).list_datastores()}


def test_la_route_est_independante_de_l_org_et_reste_hors_MCP():
    cap = registry.by_key("me.datastore.shared_with_me")
    assert cap.authz is SUB_ONLY, "aucune org requise : un partage à une personne n'en a pas"
    assert cap.mcp is None
    assert [(b.verb, b.path) for b in cap.rest_bindings()] == [("GET", "/api/me/datastores/shared")]


@pytest.mark.parametrize("org", ["x", "y", None])
def test_le_destinataire_voit_le_partage_personnel_depuis_n_importe_quelle_org(
        monde, monkeypatch, org):
    recus = _recus(monkeypatch, B, monde[org] if org else None)
    e = recus[monde["t-perso"]]
    assert e["shared_by"] == "Alice Proprio", "qui a partagé — un nom, pas un identifiant"
    assert (e["permission"], e["can_write"], e["shared"]) == ("read", False, True)
    assert (e["owner_type"], e["is_personal"]) == ("user", False)
    assert e["ns_id"] == e["id"] == monde["t-perso"]


def test_sans_doublon_avec_la_liste_de_l_org(monde, monkeypatch):
    # Contrôle positif : dans X, la liste de l'org rend DÉJÀ `t-org` (X le possède).
    assert monde["t-org"] in _liste_de_l_org(monkeypatch, B, monde["x"])
    assert monde["t-org"] not in _recus(monkeypatch, B, monde["x"]), "répété ici = doublon"
    # Hors de X, rien ne le rend ailleurs : il revient ici, avec son droit.
    e = _recus(monkeypatch, B, monde["y"])[monde["t-org"]]
    assert (e["permission"], e["can_write"]) == ("write", True)


@pytest.mark.parametrize("org", ["x", None])
def test_un_tiers_de_la_meme_org_ne_voit_rien(monde, monkeypatch, org):
    """C est dans X ET dans l'équipe : il voit les droits d'audience LÀ OÙ ILS VIVENT
    (la liste de l'org), jamais ici — ni les partages faits à B."""
    if org:
        liste = _liste_de_l_org(monkeypatch, C, monde["x"])
        assert {monde["t-a-l-org"], monde["t-a-l-equipe"]} <= liste, "contrôle positif"
    assert _recus(monkeypatch, C, monde[org] if org else None) == {}


@pytest.mark.parametrize("org", ["x", None])
def test_le_proprietaire_ne_se_voit_rien_partager(monde, monkeypatch, org):
    assert _recus(monkeypatch, A, monde[org] if org else None) == {}


def test_la_requete_ne_connait_que_le_destinataire(monde):
    assert db.list_datastores_shared_to_user(C) == []
    assert db.list_datastores_shared_to_user(A) == [], "son propre tableau n'est pas reçu"
    assert {r["id"] for r in db.list_datastores_shared_to_user(B)} == {
        monde["t-perso"], monde["t-org"]}
