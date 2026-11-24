"""La connexion menée par l'agent (`connectors/connect_link`, capacité `connector.connect`) :
chaque refus de connexion porte la page du geste qui reste, et l'agent peut la demander.

Contrat : `oto-now/connectors`, `docs/conception/refus-de-connexion.md`. Ce qui est vérifié :
l'adresse est une page du tableau de bord, sous l'org du refus, sans code ni `state` ni
jeton ; un produit qui ne déclare pas la page lève une erreur nommée, jamais un repli ; les
trois refus (connecteur coupé, pas de clé, reconnexion) portent les champs du contrat dans
`data.oto` sans rien retirer de ce qu'ils servaient ; le lien mort `/console/connections`
a disparu.
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp import links, providers
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import connect_link as CAP
from oto_mcp.connectors import activation_gate, connect_link as CL
from oto_mcp.connectors import flow as connector_flow
from oto_mcp.connectors import readiness
from oto_mcp.mcp_errors import McpError

BASE = "https://manage.oto.cx"


@pytest.fixture()
def oauth(monkeypatch):
    """`hubspot` déclare un flux de consentement (son module n'est pas importé ici)."""
    vrai = connector_flow.supports
    monkeypatch.setattr(connector_flow, "supports",
                        lambda c: c == "hubspot" or vrai(c))


# --- le geste et l'adresse ---------------------------------------------------------------

def test_le_geste_suit_le_mode_d_authentification(oauth):
    assert CL.geste("hubspot") == "oauth"
    assert CL.geste("hunter") == "key"
    assert CL.geste("whatsapp") == "hosted_link"
    assert CL.geste("web") is None          # aucun compte


def test_l_adresse_est_la_fiche_sous_l_org_du_refus():
    url = CL.adresse("hunter", sub="u1", org=7)
    assert url == f"{BASE}/o/7/connectors/hunter?space=me"
    assert CL.adresse("hunter", sub="u1", org=7, space="org") == (
        f"{BASE}/o/7/org/connectors?connector=hunter&space=org")
    assert CL.adresse("hunter", sub="u1", org=7, space="team:4") == (
        f"{BASE}/o/7/org/teams/4/connectors?connector=hunter&space=team%3A4")


@pytest.mark.parametrize("connecteur", ["hubspot", "hunter", "whatsapp"])
def test_l_adresse_ne_porte_ni_code_ni_state_ni_jeton(oauth, connecteur):
    d = CL.details("no_account", connecteur, sub="u1", org=7)
    q = parse_qs(urlsplit(d["url"]).query)
    assert set(q) <= {"space", "connector"}
    assert not urlsplit(d["url"]).fragment
    assert d["url"].startswith(BASE + "/o/7/")


def test_une_cle_se_pose_sur_la_fiche_de_son_porteur(monkeypatch):
    monkeypatch.setattr(providers, "credential_provider",
                        lambda c: "unipile" if c == "unipile_api" else c)
    monkeypatch.setattr(CL, "geste", lambda c: "key")
    d = CL.details("no_account", "unipile_api", sub="u1", org=7)
    assert d["connector"] == "unipile_api"
    assert urlsplit(d["url"]).path == "/o/7/connectors/unipile"


def test_details_du_contrat():
    d = CL.details("connector_off", "hunter", sub="u1", org=7, space="org")
    assert d == {"reason": "connector_off", "gesture": "admin_activation",
                 "connector": "hunter", "space": "org",
                 "url": f"{BASE}/o/7/org/connectors?connector=hunter&space=org"}
    with pytest.raises(ValueError):
        CL.details("no_account", "web", sub="u1", org=7)
    with pytest.raises(ValueError):
        CL.espace("team:")


def test_une_page_non_declaree_est_une_erreur_nommee(monkeypatch):
    """Un tenant qui ne déclare pas la page : erreur, jamais notre adresse à sa place."""
    entree = SimpleNamespace(link_paths={"connectors": "/c"}, dashboard_url="https://t.test")
    monkeypatch.setattr(links, "_tenant_of", lambda sub: entree)
    with pytest.raises(CL.PageDeConnexionAbsente) as e:
        CL.adresse("hunter", sub="u1", org=7)
    assert e.value.code == "connect_page_undeclared" and not e.value.declaree
    assert "OTO_DASHBOARD_LINK_PATHS" in str(e.value)
    # Le refus en cours garde son message : rien n'est ajouté, l'écart est journalisé.
    assert CL.details_du_refus("no_account", "hunter", sub="u1", org=7) == {}


def test_sans_org_pas_d_adresse_trouee():
    with pytest.raises(CL.PageDeConnexionAbsente) as e:
        CL.adresse("hunter", sub="u1", org=None)
    assert e.value.declaree


# --- les refus ----------------------------------------------------------------------------

def test_connecteur_coupe_par_l_org(monkeypatch):
    monkeypatch.setattr(activation_gate.call_axes, "current_user_sub_from_token", lambda: "u1")
    monkeypatch.setattr(activation_gate.access, "current_org", lambda sub: 7)
    monkeypatch.setattr(activation_gate.access, "current_group", lambda sub: None)
    monkeypatch.setattr(activation_gate.org_suspension, "refus", lambda org: None)
    monkeypatch.setattr(activation_gate.activation, "cran_qui_coupe", lambda c, o, g: "org")
    err = activation_gate._refus("hunter")
    # Ce qui était servi reste servi…
    assert err.data["code"] == "connector_disabled" and err.data["scope"] == "org"
    # … et le contrat s'ajoute dans `data.oto`, le message finissant par l'adresse.
    oto = err.data["oto"]
    assert oto["reason"] == "connector_off" and oto["gesture"] == "admin_activation"
    assert oto["url"] == f"{BASE}/o/7/org/connectors?connector=hunter&space=org"
    assert err.message.endswith(f"open {oto['url']}")


def test_connecteur_coupe_par_la_plateforme_sans_adresse(monkeypatch):
    monkeypatch.setattr(activation_gate.call_axes, "current_user_sub_from_token", lambda: "u1")
    monkeypatch.setattr(activation_gate.access, "current_org", lambda sub: 7)
    monkeypatch.setattr(activation_gate.access, "current_group", lambda sub: None)
    monkeypatch.setattr(activation_gate.org_suspension, "refus", lambda org: None)
    monkeypatch.setattr(activation_gate.activation, "cran_qui_coupe",
                        lambda c, o, g: "platform")
    err = activation_gate._refus("hunter")
    assert err.data["oto"] == {"code": "connector_disabled", "retryable": False}


def test_l_enveloppe_recopie_le_contrat_dans_data_oto():
    from oto_mcp.middleware import error_envelope as EE
    exc = CL.poser(McpError(ErrorData(code=INVALID_PARAMS, message="No `hunter` key.")),
                   {"reason": "no_account", "gesture": "key", "connector": "hunter",
                    "space": "me", "url": f"{BASE}/o/7/connectors/hunter?space=me"})

    async def _boom(ctx):
        raise exc

    import asyncio
    with pytest.raises(McpError) as e:
        asyncio.run(EE.ErrorEnvelopeMiddleware().on_call_tool(SimpleNamespace(), _boom))
    oto = e.value.error.data["oto"]
    assert oto["code"] == "invalid_input"           # le code servi ne bouge pas
    assert oto["gesture"] == "key" and oto["url"].endswith("/o/7/connectors/hunter?space=me")


def test_reconnexion_d_une_cle_personnelle(monkeypatch):
    from oto_mcp.middleware import error_envelope as EE
    monkeypatch.setattr(EE, "current_user_sub_from_token", lambda: "u1")
    trace = {"resolved_connector": "hunter", "key_mode": "user",
             "credential_row": ("member", "7:u1", "hunter", "")}
    d = EE._contrat_reconnexion(trace)
    assert d["reason"] == "reconnect" and d["gesture"] == "key" and d["space"] == "me"
    assert d["url"] == f"{BASE}/o/7/connectors/hunter?space=me"


def test_reconnexion_d_un_compte_prete_ou_plateforme_sans_adresse(monkeypatch):
    from oto_mcp.middleware import error_envelope as EE
    monkeypatch.setattr(EE, "current_user_sub_from_token", lambda: "u1")
    prete = {"resolved_connector": "hunter", "key_mode": "user",
             "credential_row": ("member", "7:quelqu-un", "hunter", "")}
    assert EE._contrat_reconnexion(prete) == {}
    assert EE._contrat_reconnexion({"resolved_connector": "hunter",
                                    "key_mode": "platform",
                                    "credential_row": ("platform", "k", "hunter", "")}) == {}


def test_reauth_required_est_reconnu_au_nom():
    from oto_mcp.middleware import error_envelope as EE

    class GoogleReauthRequired(RuntimeError):
        pass
    try:
        try:
            raise GoogleReauthRequired("dead")
        except GoogleReauthRequired as cause:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="reconnect")) from cause
    except McpError as e:
        assert EE._reconnexion_demandee(e)


def test_plus_de_lien_mort_console_connections():
    texte = readiness.no_identity_step("u1", "whatsapp", "account", org=7)
    assert "/console/connections" not in texte
    assert f"{BASE}/o/7/connectors/whatsapp?space=me" in texte


# --- la capacité --------------------------------------------------------------------------

_CTX = ResolvedCtx(sub="u1", org_id=7)


@pytest.fixture()
def contexte(monkeypatch):
    monkeypatch.setattr(CAP.access, "current_group", lambda sub: 4)
    monkeypatch.setattr(CAP.activation_gate, "_coupure", lambda c, o, g: None)
    monkeypatch.setattr(CAP.connector_readiness, "diagnose",
                        lambda *a, **k: SimpleNamespace(reason="no_credential"))


def test_capacite_rend_la_page_de_la_personne(contexte, oauth):
    out = CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hubspot"))
    assert out == {"gesture": "oauth", "connector": "hubspot", "space": "me", "by": "user",
                   "url": f"{BASE}/o/7/connectors/hubspot?space=me"}


def test_capacite_espace_org_par_un_administrateur(contexte, monkeypatch):
    monkeypatch.setattr(CAP.roles, "is_org_admin", lambda sub, org: False)
    out = CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter",
                                                                space="org"))
    assert out["by"] == "administrator" and out["gesture"] == "key"
    assert out["url"] == f"{BASE}/o/7/org/connectors?connector=hunter&space=org"


def test_capacite_connecteur_coupe(contexte, monkeypatch):
    monkeypatch.setattr(CAP.activation_gate, "_coupure",
                        lambda c, o, g: ("group", "disabled by your team 4", "a lead…"))
    monkeypatch.setattr(CAP.roles, "can_admin_group", lambda sub, g: True)
    out = CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter"))
    assert out["gesture"] == "admin_activation" and out["space"] == "team:4"
    assert out["by"] == "user"


def test_capacite_coupe_en_amont_refus_nomme(contexte, monkeypatch):
    monkeypatch.setattr(CAP.activation_gate, "_coupure",
                        lambda c, o, g: ("platform", "disabled by the platform", "only…"))
    with pytest.raises(AuthzDenied) as e:
        CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter"))
    assert e.value.code == "connector_cut_upstream"


def test_capacite_refus_nommes(contexte, monkeypatch):
    with pytest.raises(AuthzDenied) as e:
        CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="web"))
    assert e.value.code == "no_account_needed"
    with pytest.raises(AuthzDenied) as e:
        CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter",
                                                              access="x"))
    assert e.value.code == "access_not_declared"
    monkeypatch.setattr(CAP.roles, "can_read_group", lambda sub, g: False)
    with pytest.raises(AuthzDenied) as e:
        CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter",
                                                              space="team:99"))
    assert e.value.code == "unknown_team"
    entree = SimpleNamespace(link_paths={}, dashboard_url="https://t.test")
    monkeypatch.setattr(links, "_tenant_of", lambda sub: entree)
    with pytest.raises(AuthzDenied) as e:
        CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter"))
    assert e.value.code == "connect_page_undeclared"


def test_capacite_compte_deja_servi(contexte, monkeypatch):
    monkeypatch.setattr(CAP.connector_readiness, "diagnose", lambda *a, **k: None)
    monkeypatch.setattr(CAP, "_compte_qui_sert", lambda *a: {
        "id": "inst:12", "label": "hunter", "mode": "user",
        "owner": {"kind": "user", "name": "Ada", "yours": True}})
    out = CAP._connect_link(_CTX, CAP.ConnectorConnectLinkInput(connector="hunter"))
    assert out["gesture"] == "none" and "url" not in out
    assert out["account"]["id"] == "inst:12"


def test_proprietaire_nomme_jamais_identifie(monkeypatch):
    monkeypatch.setattr(CAP.db, "get_user", lambda sub: {"name": "Grace"})
    win = SimpleNamespace(mode="user", entity_type="member", entity_id="7:autre-sub", account="")
    owner = CAP._owner(win, "u1")
    assert owner == {"kind": "user", "name": "Grace", "yours": False}
    assert "autre-sub" not in str(owner)


def test_un_nom_qui_est_une_adresse_n_est_pas_servi(monkeypatch):
    monkeypatch.setattr(CAP.db, "get_user", lambda sub: {"name": "grace@exemple.test"})
    win = SimpleNamespace(mode="user", entity_type="member", entity_id="7:autre-sub", account="")
    assert CAP._owner(win, "u1")["name"] == "a member of your org"


def test_le_refus_sans_cle_porte_le_contrat():
    from oto_mcp.access import CredentialUnavailable
    exc = CL.sans_compte(CredentialUnavailable, "hunter", sub="u1", org=7,
                         message="No `hunter` key configured for you.")
    url = f"{BASE}/o/7/connectors/hunter?space=me"
    assert exc.error.message == f"No `hunter` key configured for you. To connect, open {url}"
    assert CL.lu(exc) == {"reason": "no_account", "gesture": "key", "connector": "hunter",
                          "space": "me", "url": url}
    # Sans org, pas d'adresse : le refus d'origine, inchangé.
    nu = CL.sans_compte(CredentialUnavailable, "hunter", sub="u1", org=None, message="No key.")
    assert nu.error.message == "No key." and CL.lu(nu) == {}
