"""Tools `aircall_*` et sonde du connecteur Aircall (lecture seule).

Ce que ce fichier verrouille — les tripwires génériques couvrent déjà registre,
éditeur, logo, dérivation des modules, prose servie et jointure au client :
- la SURFACE (7 tools, aucun verbe d'écriture) et le routage vers la bonne
  méthode du client, avec le credential à deux champs passé tel quel ;
- la vue resserrée des listes (clés nommées retirées à toute profondeur, `meta`
  intact, `projection` qui NOMME ce qui manque) et `full=True` qui rend le brut ;
- les refus d'intention (argument d'un autre `op`, argument requis absent) ;
- la traduction des refus amont, dont le 404 de l'IA conversationnelle ;
- la sonde : `probe()`, 401/403 → `NonAutorise`, autre code → non classé.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError

_CREDS = {"api_id": "id-de-test", "api_token": "jeton-de-test"}


@pytest.fixture
def client(monkeypatch):
    """Le faux client, et les kwargs de chaque construction."""
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.aircall.AircallClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(_CREDS))
    inst.built = built
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import aircall as A

    m = FastMCP("t")
    A.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body or {"error": "x"}, service="aircall")


# --- surface ------------------------------------------------------------------

def test_surface_en_lecture_seule(client):
    names = {t.name for t in asyncio.run(_mcp().list_tools())}
    assert names == {"aircall_calls", "aircall_call_ai", "aircall_users",
                     "aircall_teams", "aircall_numbers", "aircall_contacts",
                     "aircall_company"}


def test_le_registre_declare_deux_champs_dont_un_secret():
    from oto_mcp import providers
    c = providers.REGISTRY["aircall"]
    assert c.kind == "tools" and c.secret_kind == "fields"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert {(f.name, f.secret) for f in c.credential_fields} == {
        ("api_id", False), ("api_token", True)}


def test_le_credential_passe_tel_quel(client):
    client.get_company.return_value = {"company": {"name": "Acme"}}
    assert _tool("aircall_company")() == {"company": {"name": "Acme"}}
    assert client.built == [_CREDS]


# --- appels -------------------------------------------------------------------

_CALLS = {
    "meta": {"count": 1, "total": 1, "current_page": 1, "per_page": 20,
             "next_page_link": None, "previous_page_link": None},
    "calls": [{
        "id": 812, "direct_link": "https://api.aircall.test/v1/calls/812",
        "direction": "inbound", "recording": "https://rec.test/a.mp3",
        "cost": "0", "ivr_options_selected": [],
        "number": {"id": 1234, "digits": "+33 1 00 00 00 00",
                   "direct_link": "https://api.aircall.test/v1/numbers/1234",
                   "messages": {"welcome": "https://audio.test/w.mp3"}},
        "user": {"id": 456, "name": "Jane Doe", "availability_status": "available",
                 "direct_link": "https://api.aircall.test/v1/users/456"},
        "comments": [{"id": 1, "content": "rappeler",
                      "posted_by": {"id": 456, "direct_link": "x"}}],
    }],
}


def test_list_rend_la_vue_resserree_et_nomme_ce_qui_manque(client):
    client.list_calls.return_value = _CALLS
    out = _tool("aircall_calls")(date_from="2026-09-01", order="desc", per_page=50)
    kw = client.list_calls.call_args.kwargs
    assert kw["date_from"] == "2026-09-01" and kw["order"] == "desc"
    assert kw["per_page"] == 50 and kw["fetch_short_urls"] is None
    call = out["calls"][0]
    assert call["recording"] == "https://rec.test/a.mp3"
    assert "direct_link" not in call and "cost" not in call
    assert call["number"] == {"id": 1234, "digits": "+33 1 00 00 00 00"}
    assert call["user"] == {"id": 456, "name": "Jane Doe"}
    assert call["comments"][0]["posted_by"] == {"id": 456}
    assert out["meta"] == _CALLS["meta"]
    assert "messages" in out["projection"]["omitted"]
    assert "direct_link" in out["projection"]["omitted"]
    # le payload du client n'est pas muté
    assert "direct_link" in _CALLS["calls"][0]


def test_full_rend_le_brut(client):
    client.list_calls.return_value = _CALLS
    assert _tool("aircall_calls")(full=True) is _CALLS


def test_search_route_ses_filtres(client):
    client.search_calls.return_value = {"meta": {}, "calls": []}
    _tool("aircall_calls")(op="search", direction="outbound", user_id=456,
                           phone_number="+33100000000", short_urls=True)
    kw = client.search_calls.call_args.kwargs
    assert kw["direction"] == "outbound" and kw["user_id"] == 456
    assert kw["phone_number"] == "+33100000000" and kw["fetch_short_urls"] is True


def test_get_rend_l_appel_brut(client):
    client.get_call.return_value = {"call": {"id": 812, "direct_link": "x"}}
    out = _tool("aircall_calls")(op="get", call_id=812, fetch_contact=True)
    assert out == {"call": {"id": 812, "direct_link": "x"}}
    client.get_call.assert_called_once_with(812, fetch_contact=True,
                                            fetch_short_urls=None)


@pytest.mark.parametrize("kw", [
    {"op": "get"},                                    # call_id manquant
    {"op": "get", "call_id": 1, "per_page": 10},      # pagination sur un get
    {"op": "list", "user_id": 456},                   # filtre de search sur list
    {"op": "search", "call_id": 1},
])
def test_refus_d_intention(client, kw):
    with pytest.raises(McpError):
        _tool("aircall_calls")(**kw)
    assert not client.method_calls


def test_un_refus_local_du_client_devient_une_erreur_d_outil(client):
    client.list_calls.side_effect = ValueError("`per_page` must be between 1 and 50")
    with pytest.raises(McpError, match="per_page"):
        _tool("aircall_calls")(per_page=80)


# --- IA conversationnelle -----------------------------------------------------

@pytest.mark.parametrize("op,methode", [
    ("summary", "get_summary"), ("topics", "get_topics"),
    ("sentiments", "get_sentiments"), ("action_items", "get_action_items"),
])
def test_chaque_op_ia_frappe_sa_methode(client, op, methode):
    getattr(client, methode).return_value = {"ok": op}
    assert _tool("aircall_call_ai")(call_id=5, op=op) == {"ok": op}
    getattr(client, methode).assert_called_once_with(5)


def test_transcription_par_defaut_avec_son_mode(client):
    client.get_transcription.return_value = {"transcription": {}}
    _tool("aircall_call_ai")(call_id=5, mode="realtime")
    client.get_transcription.assert_called_once_with(5, mode="realtime")


def test_mode_refuse_hors_transcription(client):
    with pytest.raises(McpError):
        _tool("aircall_call_ai")(call_id=5, op="summary", mode="async")


def test_le_404_de_l_ia_parle_de_l_offre_et_de_l_analyse(client):
    client.get_summary.side_effect = _upstream(404)
    with pytest.raises(McpError) as e:
        _tool("aircall_call_ai")(call_id=5, op="summary")
    msg = str(e.value)
    assert "404" in msg and "AI" in msg and "aircall_" not in msg


# --- annuaire -----------------------------------------------------------------

def test_users_get_par_email(client):
    client.get_user.return_value = {"user": {"id": 1}}
    _tool("aircall_users")(op="get", user="jane@example.test")
    client.get_user.assert_called_once_with("jane@example.test")


def test_numbers_list_retire_les_fichiers_audio(client):
    client.list_numbers.return_value = {"meta": {}, "numbers": [
        {"id": 1, "digits": "+1", "messages": {"welcome": "u"}, "is_ivr": True,
         "users": [{"id": 2, "direct_link": "x"}]}]}
    out = _tool("aircall_numbers")()
    assert out["numbers"] == [{"id": 1, "digits": "+1", "users": [{"id": 2}]}]
    assert out["projection"]["omitted"] == ["direct_link", "is_ivr", "messages"]


def test_teams_get(client):
    client.get_team.return_value = {"team": {"id": 678}}
    assert _tool("aircall_teams")(op="get", team_id=678) == {"team": {"id": 678}}


def test_contacts_search_exige_un_critere(client):
    with pytest.raises(McpError):
        _tool("aircall_contacts")(op="search")
    client.search_contacts.return_value = {"meta": {}, "contacts": []}
    out = _tool("aircall_contacts")(op="search", email="a@example.test")
    assert out == {"meta": {}, "contacts": []}          # rien retiré → pas de notice
    assert client.search_contacts.call_args.kwargs["email"] == "a@example.test"


@pytest.mark.parametrize("status,needle", [
    (403, "API ID"), (429, "120"), (503, "unavailable"), (422, "HTTP 422"),
])
def test_traduction_des_refus_amont(client, status, needle):
    client.list_contacts.side_effect = _upstream(status)
    with pytest.raises(McpError, match=needle):
        _tool("aircall_contacts")()


# --- sonde --------------------------------------------------------------------

def _fields() -> dict:
    """Les champs EXACTEMENT comme la capacité verify les produit."""
    return credentials_store.unpack_secret(
        "aircall", credentials_store.pack_secret("aircall", dict(_CREDS)))


def test_sonde_appelle_probe(client):
    from oto_mcp.tools import aircall as A
    A._verify(_fields())
    client.probe.assert_called_once_with()
    assert client.built == [_CREDS]


@pytest.mark.parametrize("status", [401, 403])
def test_sonde_401_403_non_autorise(client, status):
    from oto_mcp.tools import aircall as A
    client.probe.side_effect = _upstream(status)
    with pytest.raises(cv.NonAutorise):
        A._verify(_fields())


def test_sonde_autre_code_non_classe(client):
    from oto_mcp.tools import aircall as A
    client.probe.side_effect = _upstream(500)
    with pytest.raises(RuntimeError) as e:
        A._verify(_fields())
    assert not isinstance(e.value, cv.SondeRefusee)
