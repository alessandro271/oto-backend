"""Ce qu'Attio a REFUSÉ doit atteindre l'agent — borné (signal #610, oto#42).

`AttioClient._request` (oto-core) rangeait le corps d'erreur d'Attio dans le message
d'une `Exception` NUE, sans statut : la taxonomie tombait en « interne » et n'écho
rien. Résultat mesuré en prod le 2026-08-28 : trois créations de company refusées en **400 `uniqueness_conflict`** — Attio nommait le champ (« slug "domains" »)
et l'enregistrement déjà porteur du domaine — rendues à l'agent en « Erreur interne du
serveur. », `retryable: false`. L'agent a conclu à un bug de suffixe `.co.uk` (faux) et
dépensé quatre appels de plus à isoler une cause qui n'existait pas.

Depuis, oto-core lève `UpstreamHTTPError` (statut, méthode, URL, `retry_after`) avec
l'enveloppe ENTIÈRE d'Attio dans `body` : ce qui reste au backend, c'est de la borner
(liste blanche) avant qu'elle n'atteigne l'agent.

**Le banc exerce la chaîne RÉELLE et lit ce que la SURFACE expose.** Rien n'est
doublé sauf le socket : le vrai tool MCP, la vraie fabrique `_client()`, le vrai
`AttioClient` d'oto-core qui lève l'erreur, puis `error_taxonomy.classify` —
c'est-à-dire exactement ce qu'`ErrorEnvelopeMiddleware` sert dans `data.oto`. Un banc
qui n'appellerait que les helpers du module passerait au vert si l'enveloppe était
retirée de `_client()`, et un banc qui s'arrêterait avant `classify` ne verrait pas
que le `scrub` de la taxonomie peut rendre le message inutilisable.

Les corps ci-dessous sont les corps RÉELS relevés dans `tool_calls.error` entre le
27/08 et le 03/09 : c'est le cas dont on connaît déjà la réponse.
"""
import asyncio
import json

import pytest
from oto.tools.common.errors import UpstreamHTTPError

from oto_mcp import error_taxonomy
from oto_mcp.tools import attio


# --- Les corps réels du journal -------------------------------------------------

CONFLIT = (
    '{"status_code":400,"type":"invalid_request_error","code":"uniqueness_conflict",'
    '"message":"A value provided for attribute with slug \\"domains\\" conflicts with '
    'one already in the system. This attribute has a uniqueness constraint. Please '
    'ensure all values for this attribute do not exist on another record. '
    'Conflicting record IDs: 00000000-0000-4000-8000-00000000c0f1"}'
)
SCOPE_MANQUANT = (
    '{"status_code":403,"type":"auth_error","code":"unauthorized","message":"The API '
    'Key provided is not authorized to perform the requested action. This request '
    'requires scopes: Read-Write access to the Records scope."}'
)
SLUG_INCONNU = (
    '{"status_code":400,"type":"invalid_request_error",'
    '"code":"unknown_filter_attribute_slug","message":"Unknown attribute slug: '
    'parent_record_id.","path":["parent_record_id"]}'
)
INTROUVABLE = (
    '{"status_code":404,"type":"invalid_request_error","code":"not_found","message":'
    '"Record with ID \\"00000000-0000-4000-8000-000000000404\\" not found."}'
)
AMONT_CASSE = "Service Unavailable"          # 503, corps NON JSON


# --- La chaîne réelle, socket excepté -------------------------------------------

class _Reponse:
    """Ce que `requests.request` rend — la seule pièce doublée de tout le banc."""

    def __init__(self, status: int, texte: str, headers: dict | None = None):
        self.status_code = status
        self.ok = status < 400
        self.text = texte
        self.content = texte.encode()
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def amont(monkeypatch):
    """Fait répondre Attio ce qu'on veut, et rend l'appelant des vrais tools.

    On ne double NI `AttioClient` (c'est lui qui lève l'erreur dont tout ce lot
    dépend) NI `_client()` (c'est là qu'est posée l'enveloppe) : seul le transport
    est substitué.
    """
    import requests
    from fastmcp import FastMCP

    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    appels: list = []
    reponse: list = []

    def _faux_request(method, url, **kw):
        appels.append((method, url))
        return reponse[0]

    monkeypatch.setattr(requests, "request", _faux_request)

    serveur = FastMCP("banc")
    attio.register(serveur)

    def _appeler(status: int, corps: str, tool: str, **kwargs):
        """Joue le tool et rend l'`ErrorInfo` que l'agent recevrait."""
        reponse[:] = [_Reponse(status, corps)]
        appels.clear()
        fn = asyncio.run(serveur.get_tool(tool)).fn
        try:
            fn(**kwargs)
        except Exception as exc:
            # NEUTRALISATION ASSERTÉE AVANT LECTURE : sans transport substitué,
            # l'appel serait parti sur le réseau et l'échec aurait une autre cause —
            # le test « passerait » sur un faux motif.
            assert appels, "le transport n'a pas été substitué : la levée ne prouve rien"
            return error_taxonomy.classify(exc)
        raise AssertionError(f"{tool} n'a pas levé sur un HTTP {status}")

    return _appeler


def _creation_refusee(amont, status, corps):
    """Le geste EXACT du signal #610 : créer une company avec un domaine."""
    return amont(status, corps, "attio_record", op="create", object="companies",
                 attributes={"name": [{"value": "Acme"}],
                             "domains": [{"domain": "acme.example"}]})


# --- Le socle : ce que l'agent lirait SANS re-typage ---------------------------

#: Une enveloppe d'erreur qui porte, hors liste blanche, des données du workspace.
ENVELOPPE_BAVARDE = {
    "status_code": 400, "type": "invalid_request_error",
    "code": "validation", "message": "Champ refusé.",
    # ce qu'on ne veut voir NULLE PART dans ce qui est servi
    "record": {"values": {"emails": ["contact@exemple-workspace.invalid"]}},
    "debug_trace": "internal-attio-stack-frame",
}


def test_sans_retypage_l_enveloppe_entiere_atteindrait_l_agent():
    """Ce qu'on ferme, épinglé sur l'erreur que lève oto-core : son message porte
    l'enveloppe ENTIÈRE, et la taxonomie la sert. Cette mesure doit rester vraie, sinon
    le test de la liste blanche ne prouve plus rien."""
    brute = UpstreamHTTPError(400, ENVELOPPE_BAVARDE, service="attio", method="POST",
                              url="https://api.attio.com/v2/objects/companies/records")
    avant = error_taxonomy.classify(brute)
    assert avant.code == "upstream_4xx"
    assert "exemple-workspace" in avant.message


# --- Ce que l'agent lit maintenant ----------------------------------------------

def test_le_refus_du_signal_610_nomme_le_champ_et_la_raison(amont):
    """Le cas exact du signal. L'agent doit lire « domains » et « uniqueness », donc
    savoir que sa requête a été REFUSÉE — pas que le serveur a planté."""
    info = _creation_refusee(amont, 400, CONFLIT)
    assert info.code == "upstream_4xx"
    assert info.retryable is False
    assert "domains" in info.message
    assert "uniqueness_conflict" in info.message
    # Un 4xx est un refus : rien n'a été écrit. C'est la réponse à la seconde question
    # du signal (« le record a-t-il été créé ? »), que l'agent payait en recherches.
    assert "400" in info.message


def test_le_refus_de_scope_dit_QUEL_droit_manque(amont):
    """Attio nomme le scope à accorder. Rendu « erreur interne », ce refus a été
    réessayé à l'identique 6 s plus tard, deux fois, en prod."""
    info = amont(403, SCOPE_MANQUANT, "attio_record", op="create", object="people",
                 attributes={"name": [{"value": "X"}]})
    assert info.code == "not_authorized"
    assert info.retryable is False
    assert "Read-Write access to the Records scope" in info.message


def test_un_slug_de_filtre_inconnu_nomme_le_champ(amont):
    """Cinq appels identiques en six heures sur quatre listes : l'agent ne pouvait
    pas apprendre le nom du champ fautif, on ne le lui disait pas."""
    info = amont(400, SLUG_INCONNU, "attio_entry", op="query", list_id_or_slug="l1",
                 filter={"parent_record_id": "r1"})
    assert info.code == "upstream_4xx"
    assert "parent_record_id" in info.message


def test_un_code_de_refus_trop_long_est_omis_plutot_que_rendu_meconnaissable(amont):
    """`unknown_filter_attribute_slug` (29 c.) tombe sur `_LONG_ID` de la taxonomie et
    se rendrait `[id]` — un caviardage en TÊTE de message, qui se lit comme un
    identifiant masqué alors que c'est le NOM du refus. Couplage déclaré dans
    `attio._MAX_CODE` : si la taxonomie change son seuil, ce test le dit."""
    info = amont(400, SLUG_INCONNU, "attio_entry", op="query", list_id_or_slug="l1")
    assert "[id]" not in info.message
    # …et un code COURT traverse en clair : même chemin, autre longueur.
    assert "uniqueness_conflict" in _creation_refusee(amont, 400, CONFLIT).message


def test_un_introuvable_est_un_introuvable(amont):
    info = amont(404, INTROUVABLE, "attio_record", op="get", object="companies",
                 record_id="r1")
    assert info.code == "not_found"
    assert info.retryable is False


def test_une_panne_amont_devient_reessayable(amont):
    """503 au corps NON JSON : ici le verdict compte plus que le texte — dire
    « interne, ne réessaie pas » sur une panne passagère fait abandonner l'agent."""
    info = amont(503, AMONT_CASSE, "attio_entry", op="query", list_id_or_slug="l1")
    assert info.code == "upstream_5xx"
    assert info.retryable is True


def test_une_limite_de_debit_dit_de_reessayer(amont):
    """Une limite de débit rendue « interne, ne réessaie pas » fait abandonner l'agent."""
    info = amont(429, "", "attio_record", op="list", object="companies")
    assert info.code == "rate_limited"
    assert info.retryable is True


def test_le_re_typage_garde_statut_methode_url_et_retry_after():
    """Le corps est réduit ; le reste de ce que porte l'erreur d'oto-core traverse
    tel quel — un appelant qui réserve avant l'appel lit `retry_after` et le statut."""
    brute = UpstreamHTTPError(429, "", service="attio", method="GET",
                              url="https://api.attio.com/v2/objects/companies/records",
                              retry_after=7.0)
    retype = attio._as_upstream(brute)
    assert (retype.status_code, retype.method, retype.retry_after) == (429, "GET", 7.0)
    assert retype.url == "https://api.attio.com/v2/objects/companies/records"


# --- Ce qui NE doit PAS traverser -----------------------------------------------

def test_seuls_code_path_et_message_traversent(amont):
    """Un corps d'erreur amont peut porter des données du workspace : on ne le relaie
    jamais en bloc. La liste blanche est la garantie — une clé qu'Attio ajouterait
    demain ne passe pas, parce qu'elle n'y est pas."""
    info = _creation_refusee(amont, 400, json.dumps(ENVELOPPE_BAVARDE))
    assert "validation" in info.message and "Champ refusé." in info.message
    assert "exemple-workspace" not in info.message
    assert "debug_trace" not in info.message and "stack-frame" not in info.message
    for interdit in ("record", "status_code", "type"):
        assert f'"{interdit}"' not in info.message


@pytest.mark.parametrize("longueur", [1500, 5000])
def test_une_phrase_amont_trop_longue_est_tronquee_et_le_dit(amont, longueur):
    """Notre borne à nous : la phrase arrive entière jusqu'ici, même au-delà de 2 000 c.
    (oto-core ne coupe plus qu'un corps non JSON), et c'est `_MAX_MESSAGE` qui la coupe."""
    info = _creation_refusee(
        amont, 400, json.dumps({"code": "x", "message": "A" * longueur}))
    assert "(truncated)" in info.message
    assert len(info.message) < 700           # borne tenue, pas la phrase entière
    assert "A" * (attio._MAX_MESSAGE + 1) not in info.message


def test_un_corps_non_json_n_est_relaye_qu_en_amorce(amont):
    page = "<!DOCTYPE html><html><head><title>error</title></head>" + "x" * 4000
    info = _creation_refusee(amont, 400, page)
    assert "non-JSON body" in info.message
    assert len(info.message) < attio._MAX_OPAQUE + 200
    assert "x" * 200 not in info.message


def test_une_erreur_qui_n_est_pas_un_refus_amont_remonte_inchangee():
    """On ne fabrique pas un statut : un bug de notre côté doit rester un bug."""
    interne = ValueError("Nothing to update — pass at least one updatable field")
    assert attio._as_upstream(interne) is None
    assert error_taxonomy.classify(interne).code == "internal"
    # Le refus d'un AUTRE service n'est pas réécrit en refus d'Attio.
    assert attio._as_upstream(UpstreamHTTPError(400, "x", service="folk")) is None


# --- Le seam et l'alarme de dérive ----------------------------------------------

def test_un_client_sans__request_est_rendu_tel_quel():
    """Dérive d'oto-core : on retombe sur le comportement d'avant ce lot (jamais une
    panne de tous les appels Attio). C'est le test suivant qui crie, pas la prod."""
    class Nu:
        pass
    nu = Nu()
    assert attio._rendre_le_refus_lisible(nu) is nu


def test_l_erreur_levee_par_oto_core_est_celle_qu_on_relit():
    """Tout ce lot repose sur deux faits d'oto-core : `AttioClient._request` existe, et il
    lève `UpstreamHTTPError` du service `attio` avec la méthode et l'URL de la requête.
    Sans ce test, un renommage rendrait le re-typage inerte **en silence** — l'enveloppe
    entière d'Attio reviendrait à l'agent sans qu'aucun autre banc ne bouge."""
    from oto.tools.attio.client import AttioClient

    assert callable(getattr(AttioClient, "_request", None)), \
        "oto-core a renommé AttioClient._request — l'enveloppe ne se pose plus"
    retype = attio._as_upstream(UpstreamHTTPError(
        422, json.loads(CONFLIT), service="attio", method="PATCH",
        url=f"{AttioClient.BASE_URL}/objects/companies/records/r1"))
    assert retype is not None, "l'erreur levée par oto-core n'est plus celle qu'on relit"
    assert retype.status_code == 422
    assert str(retype).endswith("(on PATCH /objects/companies/records/r1)")
