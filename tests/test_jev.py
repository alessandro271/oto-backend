"""Connecteur Jev — la décision typée (TypeSafe via OpenRouter).

Verrouille ce qui, sans test, se perdrait au premier lot :
- l'entrée de registre : **aucune clé de personne, aucun palier gratuit**, et le
  connecteur org-partageable UNIQUEMENT parce que c'est la porte du barreau tenant ;
- la règle qui fait tout le connecteur — **seules la clé du TENANT et le grant
  PLATEFORME sont servis** : une clé d'org est refusée EN LE DISANT, jamais servie ;
- le relevé en **micro-dollars du coût réel** (sans lui, deux cents décisions se
  relèveraient comme un appel, et le prix ne suivrait plus la dépense) ;
- la surface MCP (deux tools, chacun avec sa description — piège de la docstring
  f-string), la sonde « tester la connexion », et la tenue d'un lot : ordre rendu,
  erreur PAR état, et arrêt net sur un problème de clé ou de solde.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import jev

EXPECTED_TOOLS = {"jev_ask", "jev_items"}

NOUL = {"type": "noul", "instructions": "La condition tient-elle ?",
        "criteria": {"true": "oui", "false": "non"}}


def _reponse(cost=2.0e-05, tokens=341):
    return {"model": "typesafe/jev-1.13-20260917",
            "answers": {"q": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": tokens, "output_tokens": 21, "cost": cost}}


class _Rung:
    """Ce que rend la cascade : la clé gagnante et le BARREAU qui l'a rendue."""

    def __init__(self, mode="tenant", key="sk-or-test"):
        self.mode = mode
        self.key = key
        self.is_platform = mode == "platform"


@pytest.fixture()
def monte(monkeypatch):
    """Monte le module avec le client mocké DANS le patch (sinon le
    `from … import JevClient` de `register()` capture la vraie classe), la cascade
    au barreau tenant, et le relevé sous la main."""
    from fastmcp import FastMCP

    releve = {}
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung())
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace",
                        lambda **kw: releve.update(kw))
    monkeypatch.setattr("oto_mcp.access.record_platform_usage",
                        lambda provider, calls=1: releve.update(platform=(provider, calls)))
    with patch("oto.tools.jev.client.JevClient") as cls:
        cls.return_value.decide.return_value = _reponse()
        m = FastMCP("t")
        jev.register(m)
        tools = {t.name: t for t in asyncio.run(m._list_tools())}
        yield m, cls.return_value, tools, releve


def _fn(m, name):
    return asyncio.run(m.get_tool(name)).fn


# --- registre ---------------------------------------------------------------

def test_registre_aucune_cle_personnelle_aucun_palier_gratuit():
    c = providers.REGISTRY["jev"]
    assert c.kind == "tools" and c.keyed and c.secret_kind == "api_key"
    # `byo_user` ABSENT : une clé de décision n'est pas personnelle.
    assert c.auth_modes == frozenset({"byo_org", "platform"})
    # Pas de free tier : sans dépôt, le refus est nommé (jamais un repli silencieux).
    assert c.platform_key_open is False
    assert c.cardinality == "mono"
    assert "jev" in providers.KEY_PROVIDERS
    assert c.category == "Modèles" and c.publisher_name == "TypeSafe"


def test_org_shareable_parce_que_c_est_la_porte_du_barreau_tenant():
    """⚠️ `byo_org` n'est PAS là pour qu'une org pose sa clé : c'est la condition que
    `require_credential("tenant", …)` exige pour qu'un TENANT puisse poser la sienne.
    L'usage, lui, est fermé aux barreaux servis (test suivant)."""
    assert providers.REGISTRY["jev"].org_shareable is True


def test_doc_how_to_presente():
    kinds = {s.kind for s in providers.REGISTRY["jev"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------

def test_les_deux_tools_sont_montes_avec_leur_description(monte):
    _, _, tools, _ = monte
    assert EXPECTED_TOOLS <= set(tools)
    for nom in EXPECTED_TOOLS:
        assert tools[nom].description, f"{nom} sans description"
        assert namespace_of(nom) == "jev"


def test_sonde_verify_enregistree(monte):
    assert connector_verify.supports("jev")


# --- LA règle du connecteur : la clé de la plateforme, pas celle d'une org ---

@pytest.mark.parametrize("mode", ["org", "user", "group"])
def test_une_cle_qui_n_est_pas_celle_de_la_plateforme_est_refusee_en_le_disant(monte, monkeypatch, mode):
    m, _, _, _ = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode=mode))
    with pytest.raises(McpError) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert mode in msg and "TENANT" in msg


@pytest.mark.parametrize("mode", ["tenant", "platform"])
def test_les_barreaux_servis_passent(monte, monkeypatch, mode):
    m, client, _, _ = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode=mode))
    r = _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert r["answers"]["q"]["noul"] == 0.9
    assert r["model"] == "typesafe/jev-1.13-20260917"


# --- le relevé --------------------------------------------------------------

def test_le_releve_porte_le_cout_reel_en_microdollars(monte):
    m, client, _, releve = monte
    client.decide.return_value = _reponse(cost=2.03e-05)
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    # 20,3 µ$ → 21 : arrondi au SUPÉRIEUR, un appel qui a coûté ne se relève jamais à 0.
    assert releve["quantity"] == 21


def test_un_lot_releve_la_somme_pas_un_appel(monte):
    m, client, _, releve = monte
    client.decide.return_value = _reponse(cost=1.0e-05)
    _fn(m, "jev_items")([{"a": 1}, {"a": 2}, {"a": 3}], {"q": NOUL})
    # ⚠️ Ce cas EST la régression : `3 × 1,0e-05` vaut 3,0000000000000004e-05 en
    # binaire, et un `ceil` nu relevait 31 µ$ pour 30 dépensés — un micro-dollar
    # inventé par lot, toujours dans le même sens (cf. `_microdollars`).
    assert releve["quantity"] == 30  # 3 × 10 µ$, et non « un appel », ni 31


def test_grant_plateforme_debite_aussi_le_compteur_plateforme(monte, monkeypatch):
    m, client, _, releve = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode="platform"))
    client.decide.return_value = _reponse(cost=3.0e-05)
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert releve["platform"] == ("jev", 30)


def test_un_cout_non_declare_ne_se_facture_pas(monte):
    m, client, _, releve = monte
    client.decide.return_value = {"model": "x", "answers": {}, "usage": {}}
    _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert "quantity" not in releve


# --- la tenue d'un lot ------------------------------------------------------

def test_le_lot_rend_les_reponses_dans_l_ordre_envoye_avec_la_cle_de_l_appelant(monte):
    m, client, _, _ = monte
    r = _fn(m, "jev_items")([{"key": "a", "state": {"n": 1}},
                             {"key": "b", "state": {"n": 2}}], {"q": NOUL})
    assert [x["index"] for x in r["answers"]] == [0, 1]
    assert [x["key"] for x in r["answers"]] == ["a", "b"]
    assert r["usage"]["decided"] == 2 and r["failed"] == 0


def test_un_etat_refuse_est_une_erreur_DE_CET_ETAT_pas_du_lot(monte):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, _ = monte
    appels = {"n": 0}

    def decide(state, questions, model=None):
        appels["n"] += 1
        if state.get("n") == 2:
            raise UpstreamHTTPError(400, {"detail": {"error_type": "max_tokens_exceeded"}},
                                    service="jev")
        return _reponse()

    client.decide.side_effect = decide
    r = _fn(m, "jev_items")([{"n": 1}, {"n": 2}, {"n": 3}], {"q": NOUL})
    assert r["failed"] == 1 and r["usage"]["decided"] == 2
    rate = [x for x in r["answers"] if "error" in x][0]
    assert rate["index"] == 1 and "max_tokens_exceeded" in rate["error"]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_un_probleme_de_cle_ou_de_solde_arrete_le_lot(monte, status):
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, _ = monte
    client.decide.side_effect = UpstreamHTTPError(status, {"message": "nope"}, service="jev")
    with pytest.raises(McpError) as e:
        _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": NOUL})
    # Un seul message, pas deux cents fois le même.
    assert "clé" in str(e.value) or "Crédits" in str(e.value)


def test_le_lot_est_borne(monte):
    m, _, _, _ = monte
    with pytest.raises(McpError, match="maximum"):
        _fn(m, "jev_items")([{"n": i} for i in range(jev.MAX_ITEMS + 1)], {"q": NOUL})
    with pytest.raises(McpError, match="au moins un"):
        _fn(m, "jev_items")([], {"q": NOUL})


def test_la_grille_est_jugee_UNE_fois_pour_tout_le_lot(monte):
    m, client, _, _ = monte
    client.check_questions.side_effect = ValueError("question 'q' : `criteria` attend")
    with pytest.raises(McpError, match="criteria"):
        _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": {"type": "noul"}})
    assert client.decide.call_count == 0
