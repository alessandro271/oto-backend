"""`apollo_reveal_phone` — le seul geste d'Apollo qui ne rend pas son résultat.

Le client demandait `reveal_phone_number` et `webhook_url` : Apollo ne rend PAS
les mobiles dans la réponse, il les POSTe à une URL quelques minutes plus tard.
Ce que ces tests figent :

1. **Le reveal ne part JAMAIS sur la clé plateforme.** Apollo facture ~9 crédits
   un appel qui rend un mobile là où un match nu en coûte 1, pendant que
   `record_platform_usage` débite 1 unité : servir le reveal sur la clé commune
   ferait mentir `platform_quota`, le seul chiffre sur lequel un worker batch
   s'arrête avant le mur (oto-backend#710). Le refus s'exerce sur le GESTE RÉEL,
   et on vérifie qu'AUCUNE requête ne part (`docs/conventions.md` : « un cran ne
   se relit pas, il s'exécute »).
2. **`request_id` est servi en CHAÎNE.** C'est un entier signé 64 bits (~7,2e17),
   au-delà de la précision d'un nombre JavaScript : rendu en nombre il revient
   faux d'une unité ou deux, sans que rien ne le dise, et un id faux ne sonde
   rien.
3. **Une `webhook_url` à laquelle Apollo ne livrera jamais se refuse AVANT les
   crédits** — le reveal est facturé même quand le POST n'arrive nulle part.
4. **Pas de `next_step` qui promette un outil inutilisable** : si Apollo accepte
   sans rendre d'id, la réponse le DIT au lieu d'annoncer un sondage impossible.

Mock la CLASSE client (jamais `requests`) — cf. `tests/test_apollo_location_filters.py`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


_WEBHOOK = "https://hooks.acme.test/apollo"

# `None` est une VALEUR DE RETOUR à part entière ici (le client oto-core traduit
# le 404 d'Apollo en None) : le défaut du montage ne peut donc pas s'écrire
# `if x is None`, sinon le cas le plus intéressant devient intestable.
_DEFAUT = object()


def _mount(monkeypatch, *, byo: bool = True, match_return=_DEFAUT,
           poll_return=_DEFAUT):
    """Monte apollo.py sur un FastMCP nu. `byo=False` simule l'absence de
    credential propre (donc le palier plateforme) en faisant lever
    `resolve_credential` comme le fait la vraie résolution."""
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP
    from mcp.types import ErrorData, INVALID_PARAMS
    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import apollo as apollo_tool

    client = MagicMock()
    client.match_person.return_value = (
        {"person": {"id": "p1"}, "request_id": 718432950164203900}
        if match_return is _DEFAUT else match_return)
    client.poll_webhook_result.return_value = (
        {"done": False, "retry_after_seconds": 10}
        if poll_return is _DEFAUT else poll_return)
    usage: list[str] = []

    def _resolve(provider, want="auto", *a, **k):
        if not byo:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="Aucun credential configuré pour toi sur `apollo`"))
        return MagicMock(key="k-byo")

    monkeypatch.setattr(access, "resolve_credential", _resolve)
    monkeypatch.setattr(access, "resolve_api_key", lambda *a, **k: ("k", not byo))
    monkeypatch.setattr(access, "record_platform_usage",
                        lambda p, n=1: usage.append(p))
    monkeypatch.setattr(access, "platform_quota_hint", lambda p: None)
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)

    # `_webhook_destination` résout l'hôte : on rend le DNS déterministe plutôt
    # que de dépendre du réseau du poste (`.test` ne résout nulle part).
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    m = FastMCP("t")
    apollo_tool.register(m)
    return m, client, usage


def _tool(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --------------------------------------------------------------------------- #
# 1. Le cran : jamais sur la clé plateforme
# --------------------------------------------------------------------------- #

def test_reveal_is_refused_without_your_own_apollo_key(monkeypatch):
    from oto_mcp.mcp_errors import McpError

    m, client, usage = _mount(monkeypatch, byo=False)
    with pytest.raises(McpError):
        _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert not client.match_person.called, "aucun appel ne doit partir"
    assert usage == [], "aucun crédit plateforme ne doit être débité"


def test_polling_too_is_refused_without_your_own_key(monkeypatch):
    """Le sondage rend « les résultats de ton ÉQUIPE » : sur la clé plateforme
    mutualisée, un `request_id` deviné rendrait le lead de quelqu'un d'autre."""
    from oto_mcp.mcp_errors import McpError

    m, client, _ = _mount(monkeypatch, byo=False)
    with pytest.raises(McpError):
        _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert not client.poll_webhook_result.called


def test_the_refusal_names_the_cost_not_the_data_boundary(monkeypatch):
    """Le reste du module est byo-only pour une frontière de DONNÉES ; celui-ci
    l'est pour le COÛT. Servir le mauvais motif envoie chercher au mauvais
    endroit — c'est la phrase qu'on éprouve ici, donc on la cite (cf.
    `docs/conventions.md` : la phrase ne se cite que quand c'est elle l'objet)."""
    from oto_mcp.mcp_errors import McpError

    m, _, _ = _mount(monkeypatch, byo=False)
    with pytest.raises(McpError) as e:
        _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    msg = e.value.error.message or ""
    assert "9 crédits" in msg
    assert "séquences" not in msg, "le motif « tes propres données » ne vaut pas ici"


# --------------------------------------------------------------------------- #
# 2. Le geste nominal
# --------------------------------------------------------------------------- #

def test_reveal_forwards_the_flag_and_the_webhook(monkeypatch):
    m, client, usage = _mount(monkeypatch)
    _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    kw = client.match_person.call_args.kwargs
    assert kw["reveal_phone_number"] is True
    assert kw["webhook_url"] == _WEBHOOK
    assert kw["person_id"] == "p1"
    assert usage == [], "clé BYO : rien à débiter du pot plateforme"


def test_request_id_is_served_as_a_string(monkeypatch):
    """718432950164203900 > 2^53 : rendu en nombre, il revient faux."""
    m, _, _ = _mount(monkeypatch)
    out = _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert out["request_id"] == "718432950164203900"
    assert isinstance(out["request_id"], str)
    assert "apollo_reveal_phone_result" in out["next_step"]


def test_no_request_id_says_so_instead_of_promising_a_poll(monkeypatch):
    """Personne TROUVÉE, mais pas d'id : le reveal est parti, il n'est
    simplement pas sondable."""
    m, _, _ = _mount(monkeypatch, match_return={"person": {"id": "p1"}})
    out = _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert "request_id" not in out
    assert "Nothing to poll" in out["next_step"]
    assert out.get("matched") is not False, "une personne EST revenue"


@pytest.mark.parametrize("reponse, cas", [
    (None, "404 Apollo → le client rend None"),
    ({}, "corps vide"),
    ({"person": None}, "200 sans personne"),
])
def test_apollo_finding_nobody_is_not_an_accepted_reveal(monkeypatch, reponse, cas):
    """Le mensonge le plus cher est celui qui RASSURE : annoncer un reveal en vol
    quand Apollo n'a rien trouvé fait attendre à l'agent un POST qui ne partira
    jamais, et lui fait rendre le lead pour traité sans réessayer."""
    m, _, _ = _mount(monkeypatch, match_return=reponse)
    out = _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert out["matched"] is False, cas
    assert "request_id" not in out
    assert "No Apollo match" in out["next_step"]
    assert "accepted" not in out["next_step"], (
        "rien n'a été accepté : ni commande, ni crédit, ni webhook")


# --------------------------------------------------------------------------- #
# 3. La destination : refusée AVANT les crédits
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url, pourquoi", [
    ("http://hooks.acme.test/apollo", "Apollo ne livre qu'en HTTPS"),
    ("https://mcp.oto.cx/hook", "oto n'est pas un receveur de webhook"),
    ("https://acme.mcp.oto.cx/hook", "un SOUS-domaine d'oto est oto : il "
     "atterrit sur la même box, qui ne reçoit rien"),
    ("ftp://hooks.acme.test/apollo", "ni http ni https"),
    ("https://", "pas d'hôte"),
])
def test_an_undeliverable_webhook_is_refused_before_the_credits(
        monkeypatch, url, pourquoi):
    from oto_mcp.mcp_errors import McpError

    m, client, _ = _mount(monkeypatch)
    with pytest.raises(McpError):
        _tool(m, "apollo_reveal_phone")(webhook_url=url, person_id="p1")
    assert not client.match_person.called, f"aucun crédit ne doit partir ({pourquoi})"


def test_a_private_webhook_host_is_refused(monkeypatch):
    """Un hôte d'apparence publique qui résout en interne : Apollo livre depuis
    l'internet et n'y arrivera jamais — le reveal serait facturé pour rien."""
    import socket
    from oto_mcp.mcp_errors import McpError

    m, client, _ = _mount(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    with pytest.raises(McpError):
        _tool(m, "apollo_reveal_phone")(webhook_url=_WEBHOOK, person_id="p1")
    assert not client.match_person.called


# --------------------------------------------------------------------------- #
# 4. Le sondage
# --------------------------------------------------------------------------- #

def test_pending_poll_is_not_a_failure(monkeypatch):
    m, _, _ = _mount(monkeypatch, poll_return={"done": False,
                                               "retry_after_seconds": 10})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert out["done"] is False
    assert out["retry_after_seconds"] == 10
    assert "10s" in out["next_step"]


def test_a_finished_poll_hands_back_the_numbers(monkeypatch):
    """⚠️ L'enveloppe est celle qu'Apollo PUBLIE, pas une forme commode : les
    numéros vivent sous `webhook_result`, un cran plus bas que le raccourci
    qu'on aurait écrit spontanément. Un fixture inventé ici rendrait ce test
    vert sur une description fausse — c'est exactement ce qui était arrivé."""
    payload = {
        "request_id": 718432950164203900,
        "webhook_status": "delivered",
        "failure_reason": None,
        "webhook_result": {"people": [
            {"id": "p1", "phone_numbers": [{"sanitized_number": "+33600000000",
                                            "type_cd": "mobile"}]}]},
    }
    m, _, usage = _mount(monkeypatch, poll_return={"done": True, "result": payload})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="718432950164203900")
    assert out == {"done": True, "result": payload}
    assert usage == [], "un sondage coûte 0 crédit — rien à débiter"


def test_the_poll_description_names_the_path_the_numbers_ACTUALLY_take(monkeypatch):
    """Le chemin annoncé se lit sur le corps qu'Apollo publie, pas sur celui
    qu'on aurait aimé. Servir `result.people[]` là où il y a
    `result.webhook_result.people[]` fait lire « aucun numéro » à chaque sondage
    réussi — et une description est relue comme une instruction."""
    corps = {"webhook_result": {"people": [
        {"phone_numbers": [{"sanitized_number": "+33600000000"}]}]}}
    m, _, _ = _mount(monkeypatch, poll_return={"done": True, "result": corps})
    out = _tool(m, "apollo_reveal_phone_result")(request_id="1")
    doc = asyncio.run(m.get_tool("apollo_reveal_phone_result")).description or ""

    # On PARCOURT le chemin que la description dicte, sur la réponse servie :
    # relire les deux et les trouver d'accord ne prouve rien, les suivre si.
    chemin = doc.split("the numbers are at ")[1].split("(")[0].strip().strip("`")
    noeud = out
    for cle in chemin.split("."):
        noeud = noeud[cle.removesuffix("[]")]
        if cle.endswith("[]"):
            noeud = noeud[0]
    assert noeud, f"le chemin servi (`{chemin}`) ne mène nulle part"


# --------------------------------------------------------------------------- #
# 5. Ce que les descriptions promettent existe vraiment
# --------------------------------------------------------------------------- #

def test_the_reveal_only_names_tools_that_exist(monkeypatch):
    """Une description d'outil est relue comme une instruction : un agent qui a
    une intention et pas de destination s'en fabrique une (#613/#632)."""
    m, _, _ = _mount(monkeypatch)
    doc = asyncio.run(m.get_tool("apollo_reveal_phone")).description or ""
    for cite in ("apollo_reveal_phone_result", "apollo_search_people",
                 "apollo_match_person"):
        assert cite in doc
        assert asyncio.run(m.get_tool(cite)) is not None


def test_match_person_says_the_phone_lives_elsewhere(monkeypatch):
    """Sans ça, un agent qui cherche un mobile le demande ici, ne le trouve pas,
    et conclut que la plateforme ne sait pas faire — le signal du client."""
    m, _, _ = _mount(monkeypatch)
    doc = asyncio.run(m.get_tool("apollo_match_person")).description or ""
    assert "apollo_reveal_phone" in doc


def test_match_person_forwards_reveal_personal_emails(monkeypatch):
    m, client, _ = _mount(monkeypatch)
    _tool(m, "apollo_match_person")(person_id="p1", reveal_personal_emails=True)
    assert client.match_person.call_args.kwargs["reveal_personal_emails"] is True
