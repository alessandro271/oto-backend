"""Connecteur Resend — la liste de contacts des Broadcasts (contacts, segments, sujets).

Les tripwires génériques couvrent déjà le registre, l'éditeur, le logo, la prose
servie, la projection des listes et la jointure au client oto-core. Ce fichier
verrouille ce qui est PROPRE à ces modules :

- les trois outils montés, `resend_topic` seul déclaré en lecture ;
- chaque op appelle la BONNE méthode de `ResendClient`, avec les bons arguments ;
- la vue resserrée par défaut (`full=True` rend le brut), et le curseur suivant ;
- `unsubscribed=` sur la liste parcourt les pages et ne garde que ceux-là ;
- l'upsert en lot : reçu, plafond de 50, clés inconnues refusées, une clé refusée
  arrête le lot, une autre erreur ne coûte que sa ligne ;
- `dry_run` sur chaque écriture : lectures seules, aucune écriture ;
- la traduction des refus Resend — une clé « envoi seul » est nommée comme telle ;
- AUCUNE sonde de connexion : la clé peut n'être qu'une clé d'envoi, que
  `email_send` utilise à bon droit.

Le client est remplacé : ces tests ne dépendent pas de la version d'oto-core installée.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.mcp_errors import McpError


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None, units=1: ("re_k", False))


def _tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import resend, resend_segments

    m = FastMCP("t")
    resend.register(m)
    resend_segments.register(m)
    return {t.name: t for t in asyncio.run(m.list_tools(run_middleware=False))}


def _fn(name):
    return _tools()[name].fn


@pytest.fixture()
def client():
    with patch("oto.tools.resend.client.ResendClient") as cls:
        yield cls.return_value


def _page(rows, has_more=False):
    return {"object": "list", "has_more": has_more, "data": rows}


def _upstream(status, name, message=""):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, {"name": name, "statusCode": status,
                                      "message": message}, service="resend")


_ROW = {"id": "c-1", "email": "a@example.com", "first_name": "Ada", "last_name": None,
        "created_at": "2026-10-01 10:00:00+00", "unsubscribed": False}


# --- registre, montage --------------------------------------------------------

def test_registry_entry():
    c = providers.REGISTRY["resend"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == {"byo_org"}
    assert c.modules == ("resend", "resend_segments")
    assert "resend" in providers.EMAIL_CONNECTOR_TRANSPORT   # l'envoi route toujours


def test_trois_outils_servis_seul_le_sujet_en_lecture():
    from oto_mcp.tools.lecture import en_lecture
    tools = _tools()
    assert set(tools) == {"resend_contact", "resend_segment", "resend_topic"}
    assert all(t.description for t in tools.values())
    assert [n for n, t in tools.items() if en_lecture(t)] == ["resend_topic"]


def test_aucune_sonde_de_connexion():
    from oto_mcp.connectors import verify as connector_verify
    _tools()
    assert not connector_verify.supports("resend")


# --- lectures -----------------------------------------------------------------

def test_list_vue_resserree_et_curseur(client):
    client.list_contacts.return_value = _page([_ROW, {**_ROW, "id": "c-2"}], has_more=True)
    out = _fn("resend_contact")(segment_id="seg-1", limit=2)
    client.list_contacts.assert_called_once_with("seg-1", limit=2, after=None)
    assert out["contacts"][0] == {"id": "c-1", "email": "a@example.com",
                                  "first_name": "Ada", "unsubscribed": False,
                                  "created_at": "2026-10-01 10:00:00+00"}
    assert out["has_more"] is True and out["next_after"] == "c-2"


def test_list_full_rend_la_page_brute(client):
    client.list_contacts.return_value = _page([_ROW])
    assert _fn("resend_contact")(full=True) == _page([_ROW])


def test_list_desabonnes_parcourt_les_pages(client):
    client.list_contacts.side_effect = [
        _page([_ROW, {**_ROW, "id": "c-2", "unsubscribed": True}], has_more=True),
        _page([{**_ROW, "id": "c-3", "unsubscribed": True}]),
    ]
    out = _fn("resend_contact")(segment_id="seg-1", unsubscribed=True)
    assert [c.kwargs["after"] for c in client.list_contacts.call_args_list] == [None, "c-2"]
    assert [r["id"] for r in out["contacts"]] == ["c-2", "c-3"]
    assert out["scanned"] == 3 and out["has_more"] is False


def test_get_rend_segments_et_sujets(client):
    client.get_contact.return_value = {
        **_ROW, "object": "contact",
        "properties": {"company": {"value": "Acme", "type": "string"}}}
    client.list_contact_segments.return_value = _page(
        [{"id": "seg-1", "name": "Newsletter", "created_at": "x"}])
    client.list_contact_topics.return_value = _page(
        [{"id": "t-1", "name": "Updates", "description": "d", "subscription": "opt_out"}])
    out = _fn("resend_contact")(op="get", contact="a@example.com")
    client.get_contact.assert_called_once_with("a@example.com")
    client.list_contact_segments.assert_called_once_with("c-1", limit=100)
    assert out["properties"] == {"company": "Acme"}
    assert out["segments"] == [{"id": "seg-1", "name": "Newsletter"}]
    assert out["topics"] == [{"id": "t-1", "name": "Updates", "subscription": "opt_out"}]


# --- upsert ---------------------------------------------------------------------

def test_upsert_un_contact(client):
    client.upsert_contact.return_value = {"email": "a@example.com", "action": "created"}
    _fn("resend_contact")(op="upsert", email="a@example.com", first_name="Ada",
                          segment_ids=["seg-1"], dry_run=True)
    client.upsert_contact.assert_called_once_with(
        "a@example.com", first_name="Ada", last_name=None, unsubscribed=None,
        properties=None, segment_ids=["seg-1"], dry_run=True)


def test_upsert_en_lot_rend_un_recu(client):
    client.upsert_contact.side_effect = [
        {"email": "a@example.com", "action": "created", "unsubscribed": False},
        {"email": "b@example.com", "action": "unchanged", "unsubscribed": True,
         "kept_unsubscribed": True},
        _upstream(422, "validation_error", "bad property"),
        {"email": "d@example.com", "action": "updated", "unsubscribed": False},
    ]
    out = _fn("resend_contact")(op="upsert", segment_ids=["seg-1"], contacts=[
        {"email": "a@example.com"}, {"email": "b@example.com", "unsubscribed": False},
        {"email": "c@example.com", "properties": {"x": "1"}}, {"email": "d@example.com"}])
    assert out["total"] == 4 and out["succeeded"] == 3
    assert out["created"] == ["a@example.com"] and out["updated"] == ["d@example.com"]
    assert out["unchanged"] == ["b@example.com"]
    assert out["kept_unsubscribed"] == ["b@example.com"]
    assert out["failed"][0]["index"] == 2 and "bad property" in out["failed"][0]["error"]
    assert client.upsert_contact.call_args_list[1].kwargs["unsubscribed"] is False


def test_upsert_en_lot_une_cle_refusee_arrete_tout(client):
    client.upsert_contact.side_effect = _upstream(
        401, "restricted_api_key", "This API key is restricted to only send emails")
    with pytest.raises(McpError, match="Full access"):
        _fn("resend_contact")(op="upsert", contacts=[{"email": "a@example.com"},
                                                     {"email": "b@example.com"}])
    assert client.upsert_contact.call_count == 1


@pytest.mark.parametrize("kwargs, match", [
    ({"contacts": [{"email": f"u{i}@example.com"} for i in range(51)]}, "at most 50"),
    ({"contacts": [{"email": "a@example.com", "firstName": "x"}]}, "unknown key"),
    ({"contacts": [{"first_name": "x"}]}, "email"),
    ({"contacts": [{"email": "a@example.com"}], "email": "b@example.com"}, "not both"),
    ({}, "email"),
])
def test_upsert_refus_avant_tout_appel(client, kwargs, match):
    with pytest.raises(McpError, match=match):
        _fn("resend_contact")(op="upsert", **kwargs)
    client.upsert_contact.assert_not_called()


def test_upsert_en_lot_dry_run(client):
    client.upsert_contact.return_value = {
        "email": "a@example.com", "action": "would_update", "dry_run": True,
        "changes": {"first_name": {"from": None, "to": "Ada"}}, "unsubscribed": False}
    out = _fn("resend_contact")(op="upsert", dry_run=True,
                                contacts=[{"email": "a@example.com", "first_name": "Ada"}])
    assert out["dry_run"] is True
    assert out["would_update"] == [{"email": "a@example.com", "changes": {
        "first_name": {"from": None, "to": "Ada"}}}]
    assert client.upsert_contact.call_args.kwargs["dry_run"] is True


# --- autres écritures ---------------------------------------------------------------

def test_update_n_envoie_que_les_champs_donnes(client):
    client.update_contact.return_value = {"object": "contact", "id": "c-1"}
    out = _fn("resend_contact")(op="update", contact="a@example.com", unsubscribed=True)
    client.update_contact.assert_called_once_with("a@example.com", unsubscribed=True)
    assert out == {"id": "c-1", "updated": ["unsubscribed"]}


def test_update_dry_run_lit_et_n_ecrit_rien(client):
    client.get_contact.return_value = {
        **_ROW, "properties": {"plan": {"value": "free", "type": "string"}}}
    out = _fn("resend_contact")(op="update", contact="c-1", dry_run=True,
                                first_name="Ada L.", properties={"plan": "pro"})
    client.update_contact.assert_not_called()
    assert out["current"] == {"first_name": "Ada", "properties": {"plan": "free"}}
    assert out["would_set"] == {"first_name": "Ada L.", "properties": {"plan": "pro"}}


def test_update_sans_champ_refuse(client):
    with pytest.raises(McpError, match="at least one"):
        _fn("resend_contact")(op="update", contact="c-1")


def test_delete_et_son_dry_run(client):
    client.get_contact.return_value = _ROW
    out = _fn("resend_contact")(op="delete", contact="c-1", dry_run=True)
    client.delete_contact.assert_not_called()
    assert out["would_delete"]["email"] == "a@example.com"
    client.delete_contact.return_value = {"object": "contact", "contact": "c-1",
                                          "deleted": True}
    assert _fn("resend_contact")(op="delete", contact="c-1") == {"contact": "c-1",
                                                                 "deleted": True}


def test_appartenance_a_un_segment(client):
    out = _fn("resend_contact")(op="add_to_segment", contact="a@example.com",
                                segment_id="seg-1", dry_run=True)
    assert out == {"dry_run": True, "would_add": {"contact": "a@example.com",
                                                  "segment_id": "seg-1"}}
    client.add_contact_to_segment.assert_not_called()
    _fn("resend_contact")(op="add_to_segment", contact="a@example.com", segment_id="seg-1")
    client.add_contact_to_segment.assert_called_once_with("a@example.com", "seg-1")
    _fn("resend_contact")(op="remove_from_segment", contact="c-1", segment_id="seg-1")
    client.remove_contact_from_segment.assert_called_once_with("c-1", "seg-1")


# --- segments, sujets -----------------------------------------------------------------

def test_segments(client):
    client.list_segments.return_value = _page([{"id": "seg-1", "name": "N",
                                                "created_at": "x"}])
    out = _fn("resend_segment")(limit=10, after="seg-0")
    client.list_segments.assert_called_once_with(limit=10, after="seg-0")
    assert out == {"segments": [{"id": "seg-1", "name": "N", "created_at": "x"}],
                   "has_more": False, "next_after": None}
    assert _fn("resend_segment")(op="create", name="N", dry_run=True) == {
        "dry_run": True, "would_create": {"name": "N"}}
    client.create_segment.assert_not_called()
    client.create_segment.return_value = {"object": "segment", "id": "seg-2", "name": "N"}
    assert _fn("resend_segment")(op="create", name="N") == {"id": "seg-2", "name": "N"}


def test_sujets(client):
    client.list_topics.return_value = _page([{"id": "t-1", "name": "Updates",
                                              "default_subscription": "opt_in"}])
    out = _fn("resend_topic")()
    client.list_topics.assert_called_once_with(limit=50, after=None)
    assert out["topics"] == [{"id": "t-1", "name": "Updates",
                              "default_subscription": "opt_in"}]


# --- erreurs ---------------------------------------------------------------------------

def test_une_cle_d_envoi_seul_est_nommee(client):
    client.list_segments.side_effect = _upstream(
        401, "restricted_api_key", "This API key is restricted to only send emails")
    with pytest.raises(McpError) as e:
        _fn("resend_segment")()
    msg = str(e.value)
    assert "only SEND" in msg and "Full access" in msg and "keeps working" in msg


def test_une_cle_inconnue_est_nommee(client):
    client.list_contacts.side_effect = _upstream(400, "validation_error",
                                                 "API key is invalid")
    with pytest.raises(McpError, match="rejected the key"):
        _fn("resend_contact")()


def test_introuvable(client):
    client.get_contact.side_effect = _upstream(404, "not_found", "Contact not found")
    with pytest.raises(McpError, match="id or its email"):
        _fn("resend_contact")(op="get", contact="nobody@example.com")


def test_refus_local_du_client_rendu_en_erreur_d_outil(client):
    client.list_contacts.side_effect = ValueError("`limit` must be an integer between 1 and 100")
    with pytest.raises(McpError, match="between 1 and 100"):
        _fn("resend_contact")(limit=500)
