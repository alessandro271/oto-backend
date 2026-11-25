"""Les exceptions nommées de la garde d'egress (`OTO_EGRESS_ALLOW`) sont liées à leur
TITULAIRE, et chaque appel sortant dit au nom de qui il sort.

- La grammaire : `nom=adresse:port[@palier:id[,palier:id…]]` ; sans `@` = la
  plateforme seule (le format d'avant, toujours lisible) ; une entrée mal formée
  refuse le chargement.
- La règle : un identifiant ne reçoit que les exceptions qui nomment EXACTEMENT son
  palier et son id — une équipe n'hérite pas de son org ni l'inverse, un membre de
  rien ; la plateforme reçoit celles sans `@` ; un appel sans titulaire n'en reçoit
  aucune, et un appel qui ne dit rien ne s'exécute pas.
- Par famille de connecteurs : l'identifiant d'un membre ou d'une autre org est
  refusé vers une exception, le titulaire exact l'atteint, la plateforme atteint une
  exception sans `@`.

Aucun accès réseau : la résolution passe par la couture `egress.resolved_addresses`,
et les clients de la lib sont remplacés par un double qui ne sort pas.
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from oto_mcp import egress, egress_transport

INTERNE = "127.0.0.1"
#: Le pont du titulaire (port 443) et celui de la plateforme (port 8443).
ALLOW = (f"pont-equipe={INTERNE}:443@group:3,org:9, "
         f"pont-plateforme={INTERNE}:8443")

GROUPE = ("group", "3")
ORG_TITULAIRE = ("org", "9")
AUTRE_ORG = ("org", "1")
ORG_DU_GROUPE = ("org", "7")     # l'org du groupe 3 : n'hérite pas de son groupe
MEMBRE = ("member", "7:sub")     # un membre de cette org : n'hérite de rien
PLATEFORME = egress.PLATEFORME


@pytest.fixture(autouse=True)
def pont_declare(monkeypatch):
    monkeypatch.setenv(egress.ALLOW_VAR, ALLOW)
    monkeypatch.setattr(egress, "resolved_addresses", lambda hote, port: {INTERNE})


def _ligne(t):
    """`instance` d'une sonde / `row` d'une carte : `(entity_type, entity_id, account)`."""
    return (t[0], t[1], "")


# ── 1. la grammaire ───────────────────────────────────────────────────────────

def test_la_grammaire_lit_titulaires_multiples_et_ancien_format():
    lues = {e.nom: e for e in egress._lire_exceptions(
        "a=127.0.0.1:1@group:3,org:9\nb=10.0.0.5:2, c=[::1]:3@tenant:acme")}
    assert lues["a"].titulaires == {("group", "3"), ("org", "9")}
    assert (lues["b"].adresse, lues["b"].port, lues["b"].titulaires) == \
        ("10.0.0.5", 2, frozenset())
    assert lues["c"].titulaires == {("tenant", "acme")} and lues["c"].adresse == "::1"


@pytest.mark.parametrize("brut", [
    "pont=127.0.0.1",                    # pas de port
    "pont=pont.local:9000",              # un nom d'hôte
    "pont=127.0.0.1:9000@",              # `@` sans titulaire
    "pont=127.0.0.1:9000@group",         # palier sans id
    "pont=127.0.0.1:9000@member:7:sub",  # un membre ne se nomme pas
    "pont=127.0.0.1:9000@user:42",       # palier inconnu
    "pont=127.0.0.1:9000@group:3 4",     # id avec espace
    "pont=127.0.0.1:9000, org:9",        # titulaire orphelin (pas de `@` avant)
    "127.0.0.1:9000",                    # pas de nom
])
def test_une_entree_mal_formee_est_refusee_au_chargement(monkeypatch, brut):
    monkeypatch.setenv(egress.ALLOW_VAR, brut)
    with pytest.raises(ValueError, match=egress.ALLOW_VAR):
        egress.verifier_reglages()
    with pytest.raises(ValueError, match=egress.ALLOW_VAR):
        egress.declared_exceptions(titulaire=PLATEFORME)


def test_le_demarrage_verifie_la_variable():
    """`verifier_reglages` est appelé au boot du service, à côté des autres
    vérifications de réglages."""
    import inspect

    from oto_mcp import server
    assert "egress.verifier_reglages()" in inspect.getsource(server)


def test_une_variable_bien_formee_passe_le_chargement(monkeypatch):
    monkeypatch.setenv(egress.ALLOW_VAR, ALLOW)
    egress.verifier_reglages()
    monkeypatch.delenv(egress.ALLOW_VAR)
    egress.verifier_reglages()


# ── 2. la règle ───────────────────────────────────────────────────────────────

def test_chaque_titulaire_recoit_exactement_ses_exceptions():
    def noms(t):
        return set(egress.declared_exceptions(titulaire=t).values())
    assert noms(GROUPE) == {"pont-equipe"}
    assert noms(ORG_TITULAIRE) == {"pont-equipe"}
    assert noms(PLATEFORME) == {"pont-plateforme"}, "sans `@` = la plateforme seule"
    for t in (AUTRE_ORG, ORG_DU_GROUPE, MEMBRE, ("group", "30"), ("tenant", "3"),
              egress.AUCUN, egress.AGENT):
        assert noms(t) == set(), t


def test_titulaire_de_suit_la_ligne_du_coffre():
    assert egress.titulaire_de("platform", "cle") == PLATEFORME
    assert egress.titulaire_de("group", 3) == GROUPE
    assert egress.titulaire_de("member", "7:sub") == MEMBRE
    assert egress.titulaire_de(None, None) == egress.AUCUN
    assert egress.titulaire_d_instance(None) == egress.AUCUN
    assert egress.titulaire_des_champs({"base_url": "x"}) == egress.AUCUN


def test_un_appel_sans_titulaire_ne_s_execute_pas():
    with pytest.raises(TypeError):
        egress.check_url("https://pont.test", connector="api")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        egress_transport.session(connector="api")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        egress_transport.harden(requests.Session(), connector="api")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        egress_transport.pinned(_Double(), connector="api")  # type: ignore[call-arg]
    with pytest.raises(TypeError):   # l'ancienne politique booléenne ne passe plus
        egress.check_url("https://pont.test", connector="api", titulaire=True)


def test_un_appel_sans_ligne_n_a_aucune_exception():
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        egress.check_url("https://pont.test", connector="api", titulaire=egress.AUCUN)


def test_une_url_d_agent_n_a_aucune_exception():
    with pytest.raises(egress.EgressRefused, match="sans exception"):
        egress.check_url("https://pont.test:8443", connector="web_read", field="url",
                         titulaire=egress.AGENT)


def test_le_refus_propose_l_entree_du_titulaire():
    with pytest.raises(egress.EgressRefused) as e:
        egress.check_url("https://pont.test:9000", connector="api",
                         titulaire=("group", "5"))
    assert f"mon-pont={INTERNE}:9000@group:5" in str(e.value)
    with pytest.raises(egress.EgressRefused) as e:
        egress.check_url("https://pont.test:9000", connector="api", titulaire=MEMBRE)
    assert "clé personnelle de membre" in str(e.value)


# ── 3. par famille ────────────────────────────────────────────────────────────

class _Double:
    """Un client de la lib qui ne sort pas : construit = le contrôle a laissé passer."""

    def __init__(self, *a, **k):
        self.session = requests.Session()

    def __getattr__(self, nom):
        return lambda *a, **k: {"id": 1, "login": "x"}


_SONDES = {
    "wordpress": ("oto.tools.wordpress.WordPressClient",
                  lambda url: {"site_url": url, "username": "u",
                               "application_password": "p"}, {}),
    "lighton": ("oto.tools.lighton.LightOnClient",
                lambda url: {"api_key": "k", "base_url": url}, {}),
    "salesforce": ("oto.tools.salesforce.client.SalesforceClient",
                   lambda url: {"client_id": "i", "client_secret": "s",
                                "refresh_token": "r", "login_url": url}, {}),
    "threecx": ("oto.tools.threecx.ThreeCXClient",
                lambda url: {"base_url": url, "auth_mode": "api_client", "client_id": "i",
                             "client_secret": "s"},
                {}),
    "n8n": ("oto.tools.n8n.N8nClient", lambda url: {"api_key": "k", "base_url": url},
            {}),
    "make": ("oto.tools.make.MakeClient",
             lambda url: {"api_token": "k", "base_url": url}, {}),
    "github": ("oto.tools.github.client.GitHubClient",
               lambda url: {"token": "t", "base_url": url}, {}),
    "posthog": ("oto.tools.posthog.client.PostHogClient",
                lambda url: {"api_key": "k"}, "host"),
    "clay": (None, lambda url: {"kind": "table",
                                "webhook": "https://api.clay.com/v3/sources/webhook/x"},
             {}),
}


def _sonde(monkeypatch, nom: str, url: str):
    import importlib
    module = importlib.import_module(f"oto_mcp.tools.{nom}")
    classe, champs, config = _SONDES[nom]
    if classe:
        monkeypatch.setattr(classe, _Double)
    config = {"host": url} if config == "host" else config
    return lambda instance: module._verify(champs(url), config, instance=instance)


@pytest.mark.parametrize("nom", sorted(_SONDES))
def test_chaque_sonde_suit_le_titulaire_de_sa_ligne(monkeypatch, nom):
    pont = "https://api.clay.com" if nom == "clay" else "https://pont.exemple.test"
    sonde = _sonde(monkeypatch, nom, pont)
    for t in (MEMBRE, AUTRE_ORG, ORG_DU_GROUPE):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            sonde(_ligne(t))
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        sonde(None)                                   # clé en cours de pose
    sonde(_ligne(GROUPE))
    sonde(_ligne(ORG_TITULAIRE))
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        sonde(_ligne(PLATEFORME))                     # pont du titulaire, pas le sien
    if nom != "clay":                                 # son hôte est fixé par la lib
        _sonde(monkeypatch, nom, "https://pont.exemple.test:8443")(("platform", "c", ""))


def test_lucca_suit_le_titulaire_de_sa_ligne(monkeypatch):
    """Lucca construit son hôte depuis un domaine : la résolution le remplace."""
    from oto_mcp.tools import lucca
    monkeypatch.setattr("oto.tools.lucca.LuccaClient", _Double)
    champs = {"api_key": "k", "domain": "acme"}
    for t in (MEMBRE, AUTRE_ORG):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            lucca._verify(champs, instance=_ligne(t))
    lucca._verify(champs, instance=_ligne(GROUPE))


@pytest.mark.parametrize("mode,champs", [
    ("bearer", {"base_url": "https://pont.exemple.test", "auth_mode": "bearer",
                "token": "t"}),
    ("oauth2", {"base_url": "https://public.exemple.test", "auth_mode": "oauth2",
                "token_url": "https://pont.exemple.test/token",
                "client_id": "i", "client_secret": "s"}),
])
def test_la_carte_api_suit_le_titulaire_de_sa_ligne(monkeypatch, mode, champs):
    from oto_mcp.tools import api as tool_api
    monkeypatch.setattr(egress, "resolved_addresses",
                        lambda hote, port: {"93.184.216.34"} if hote.startswith("public.")
                        else {INTERNE})
    for row in (MEMBRE, AUTRE_ORG, ORG_DU_GROUPE, None):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            tool_api.client_for(champs, row=_ligne(row) if row else None)
    tool_api.client_for(champs, row=_ligne(GROUPE))


def test_la_carte_api_de_la_plateforme_atteint_une_exception_sans_arobase():
    from oto_mcp.tools import api as tool_api
    champs = {"base_url": "https://pont.exemple.test:8443", "auth_mode": "bearer",
              "token": "t"}
    tool_api.client_for(champs, row=("platform", "cle", ""))
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        tool_api.client_for(champs, row=_ligne(GROUPE))


def test_le_serveur_mcp_distant_suit_le_titulaire_de_la_carte(monkeypatch):
    """Le serveur MCP distant d'une carte `api` : celle d'un membre n'atteint pas
    un pont, même de son org ; celle du groupe titulaire l'atteint."""
    from oto_mcp.connectors import api_mcp
    monkeypatch.setattr(api_mcp, "_Client", _ClientMcp)
    champs = {"base_url": "https://pont.exemple.test/mcp", "auth_mode": "bearer",
              "token": "t"}
    for row in (MEMBRE, AUTRE_ORG, None):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            api_mcp._ouvrir(champs, _ligne(row) if row else None)
    api_mcp._ouvrir(champs, _ligne(GROUPE))


class _ClientMcp:
    def __init__(self, url, session):
        self.s = session

    def request(self, *a, **k):
        return {}

    def notify(self, *a, **k):
        return None


def test_le_login_du_mode_session_suit_le_titulaire():
    from oto_mcp.connectors import api_session
    url = "https://pont.exemple.test/login"
    for t in (MEMBRE, AUTRE_ORG):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            api_session.guard("https://pont.exemple.test", titulaire=t)(url)
    api_session.guard("https://pont.exemple.test", titulaire=GROUPE)(url)
    with pytest.raises(egress.EgressRefused, match="destination refusée"):
        # un saut sur un AUTRE hôte que celui de la carte : aucune exception
        api_session.guard("https://carte.exemple.test", titulaire=GROUPE)(url)


class _Passe(Exception):
    """Le contrôle précoce a laissé passer : on s'arrête avant le réseau."""


def test_le_jeton_oauth_d_une_carte_suit_le_titulaire(monkeypatch):
    from oto_mcp.auth import api_oauth, flow

    class _S:
        def post(self, *a, **k):
            raise _Passe

    monkeypatch.setattr(api_oauth, "_session", lambda titulaire: _S())
    url = "https://pont.exemple.test/token"
    for t in (MEMBRE, AUTRE_ORG, egress.AUCUN):
        with pytest.raises(flow.OAuthExchangeRefused, match="destination refusée"):
            api_oauth._token_request(url, {}, annoncee=False, titulaire=t)
    with pytest.raises(_Passe):
        api_oauth._token_request(url, {}, annoncee=False, titulaire=GROUPE)
    with pytest.raises(flow.OAuthExchangeRefused, match="destination refusée"):
        # une URL annoncée par le serveur distant : aucune exception
        api_oauth._token_request(url, {}, annoncee=True, titulaire=GROUPE)


def test_la_decouverte_d_une_app_suit_l_org_de_l_app(monkeypatch):
    from oto_mcp.auth import mcp_discovery

    def passe(*a, **k):
        raise _Passe
    monkeypatch.setattr(mcp_discovery, "_resource_metadata_url", passe)
    url = "https://pont.exemple.test/mcp"
    for t in (AUTRE_ORG, ORG_DU_GROUPE, MEMBRE):
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            mcp_discovery.discover(url, titulaire=t)
    with pytest.raises(_Passe):
        mcp_discovery.discover(url, titulaire=ORG_TITULAIRE)
    assert mcp_discovery._TITULAIRE.get() == egress.AUCUN, "rendu après la découverte"


def test_les_outils_sortent_au_nom_de_la_ligne_resolue(monkeypatch):
    """Les outils lisent leurs champs par `access.resolve_credential_fields`, qui
    porte le titulaire de la ligne gagnante."""
    from types import SimpleNamespace

    from oto_mcp.access import resolve, views
    from oto_mcp.tools import threecx

    def resolu(t):
        return lambda provider, want=None, account=None: SimpleNamespace(
            fields={"base_url": "https://pont.exemple.test", "auth_mode": "api_client",
                    "client_id": "i",
                    "client_secret": "s"},
            entity_type=t[0], entity_id=t[1])

    monkeypatch.setattr("oto.tools.threecx.ThreeCXClient", _Double)
    monkeypatch.setattr(resolve, "resolve_credential", resolu(GROUPE))
    assert egress.titulaire_des_champs(views.resolve_credential_fields("threecx")) == GROUPE
    threecx.client()
    for t in (MEMBRE, AUTRE_ORG):
        monkeypatch.setattr(resolve, "resolve_credential", resolu(t))
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            threecx.client()


def test_les_outils_lighton_sortent_au_nom_de_la_ligne_resolue(monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp.access import resolved_credential
    from oto_mcp.tools import lighton

    def champs(t):
        c = resolved_credential.ChampsResolus(
            {"api_key": "k", "base_url": "https://pont.exemple.test"})
        c.titulaire = t
        return lambda provider, account=None: c

    m = FastMCP("t")
    lighton.register(m)
    outil = asyncio.run(m.get_tool("lighton_search")).fn
    for t in (MEMBRE, AUTRE_ORG):
        monkeypatch.setattr(lighton.access, "resolve_credential_fields", champs(t))
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            outil(query="x")


# ── 4. le transport : même règle, même titulaire, à la connexion ──────────────

class _Resolveur:
    """Une adresse publique au contrôle, le pont à la connexion (rebinding)."""

    def __init__(self):
        self.appels = 0

    def __call__(self, hote, port):  # noqa: ARG002
        self.appels += 1
        return {"93.184.216.34"} if self.appels == 1 else {INTERNE}


@pytest.fixture
def aucune_socket(monkeypatch):
    def ouvre(*a, **k):
        raise _Passe
    monkeypatch.setattr(egress_transport, "_ouvrir", ouvre)


@pytest.mark.parametrize("titulaire,admis", [
    (MEMBRE, False), (AUTRE_ORG, False), (ORG_DU_GROUPE, False),
    (egress.AUCUN, False), (egress.AGENT, False), (PLATEFORME, False),
    (GROUPE, True), (ORG_TITULAIRE, True),
])
def test_le_rebinding_est_juge_avec_le_titulaire_de_la_session(monkeypatch, aucune_socket,
                                                               titulaire, admis):
    resolveur = _Resolveur()
    monkeypatch.setattr(egress, "resolved_addresses", resolveur)
    url = "https://rebond.exemple.test/"
    egress.check_url(url, connector="api", titulaire=titulaire)   # public au contrôle
    s = egress_transport.session(connector="api", field="base_url", titulaire=titulaire)
    if admis:
        with pytest.raises((_Passe, requests.ConnectionError)):   # jusqu'à la socket
            s.get(url, timeout=5)
    else:
        with pytest.raises(egress.EgressRefused, match="destination refusée"):
            s.get(url, timeout=5)
    assert resolveur.appels == 2, "le transport résout lui-même, une fois"


# ── 5. aucun appelant ne reste sur un défaut ──────────────────────────────────

_GARDEES = {"check_url", "decide", "declared_exceptions", "harden", "pinned",
            "http_allowed", "site_session"}


def _appels_sans_titulaire(source: str, chemin: str) -> list[str]:
    import ast
    fautes = []
    for n in ast.walk(ast.parse(source)):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        base = ast.unparse(n.func.value)
        nom = n.func.attr
        garde = (nom in _GARDEES and base in ("egress", "egress_transport", "wp_auth")
                 or (nom == "session" and base == "egress_transport"))
        if garde and "titulaire" not in {k.arg for k in n.keywords}:
            fautes.append(f"{chemin}:{n.lineno} {base}.{nom}")
    return fautes


def test_chaque_appel_sortant_dit_son_titulaire():
    """Le grep exhaustif, en garde : chaque appel de la garde d'egress dans le code
    servi passe un `titulaire` explicite (la signature l'exige déjà ; ce cran-ci
    attrape aussi un chemin qu'aucune épreuve n'exécute)."""
    import pathlib
    racine = pathlib.Path(egress.__file__).parent
    fautes = []
    for f in sorted(racine.rglob("*.py")):
        fautes += _appels_sans_titulaire(f.read_text(encoding="utf-8"),
                                         str(f.relative_to(racine.parent)))
    assert not fautes, "appel(s) de la garde d'egress sans titulaire :\n  " + \
        "\n  ".join(fautes)


def test_le_detecteur_de_titulaire_rougit_sur_commande():
    src = ("from . import egress, egress_transport\n"
           "egress.check_url(u, connector='n8n')\n"
           "egress_transport.session(connector='api', field='url')\n"
           "egress.check_url(u, connector='n8n', titulaire=t)\n")
    assert _appels_sans_titulaire(src, "x") == ["x:2 egress.check_url",
                                                "x:3 egress_transport.session"]
