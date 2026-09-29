"""Connecteur Jev — la décision typée (TypeSafe via OpenRouter).

Verrouille ce qui, sans test, se perdrait au premier lot :
- l'entrée de registre : **aucune clé de personne, aucune clé plateforme, aucun
  palier gratuit**, et le connecteur org-partageable UNIQUEMENT parce que c'est la
  porte du barreau tenant ;
- la règle qui fait tout le connecteur — **seule la clé du TENANT est servie** : une
  clé d'org, d'équipe ou de personne la masque, et elle est refusée EN LE DISANT (et
  en nommant qui la retire), jamais servie ; sans clé, le refus dit la seule voie ;
- le relevé en **micro-dollars du coût réel** (sans lui, deux cents décisions se
  relèveraient comme un appel, et le prix ne suivrait plus la dépense) ;
- la surface MCP (deux tools, chacun avec sa description — piège de la docstring
  f-string), la sonde « tester la connexion », et la tenue d'un lot : ordre rendu,
  erreur PAR état, arrêt net sur un problème de clé ou de solde SANS perdre le relevé
  de ce qui est déjà décidé, fenêtre de départ (les états non partis sont à rejouer),
  et borne de taille d'un état.
"""
import asyncio
import time
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

    def resoudre(provider, want="auto", **kw):
        releve["units"] = kw.get("units")
        return _Rung()

    monkeypatch.setattr("oto_mcp.access.resolve_credential", resoudre)
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace",
                        lambda **kw: releve.update(kw))
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
    # `byo_user` ABSENT : une clé de décision n'est pas personnelle. `platform`
    # ABSENT : oto ne paie pas jev, le tenant apporte sa clé (décision du 28/09/2026).
    assert c.auth_modes == frozenset({"byo_org"})
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


def test_la_sonde_ne_passe_pas_au_vert_pour_une_cle_qui_sera_refusee(monte):
    """Une carte d'org verte pour une clé que l'usage refusera mentirait : la sonde
    échoue SANS appel quand la ligne sondée n'est pas celle du tenant."""
    with patch("oto.tools.jev.client.JevClient") as cls:
        with pytest.raises(ValueError, match="TENANT"):
            jev._verify({"key": "sk-or-x"}, {}, instance=("org", "7", ""))
        assert cls.call_count == 0
        jev._verify({"key": "sk-or-x"}, {}, instance=("tenant", "pilote", ""))
        jev._verify({"key": "sk-or-x"}, {})  # avant dépôt : la clé seule
        assert cls.return_value.decide.call_count == 2


# --- LA règle du connecteur : la clé du tenant, et elle seule -----------------

@pytest.mark.parametrize("mode, qui", [("org", "administrateur de l'org"),
                                       ("user", "titulaire"),
                                       ("group", "administrateur de l'équipe"),
                                       ("platform", "celui qui l'a posée")])
def test_une_cle_qui_masque_celle_du_tenant_est_refusee_en_nommant_qui_la_retire(
        monte, monkeypatch, mode, qui):
    m, client, _, _ = monte
    monkeypatch.setattr("oto_mcp.access.resolve_credential",
                        lambda provider, want="auto", **kw: _Rung(mode=mode))
    with pytest.raises(McpError) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert mode in msg and "masque" in msg and "TENANT" in msg and qui in msg
    assert client.decide.call_count == 0


def test_le_barreau_tenant_passe(monte):
    m, client, _, _ = monte
    r = _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    assert r["answers"]["q"]["noul"] == 0.9
    assert r["model"] == "typesafe/jev-1.13-20260917"


def test_sans_cle_le_refus_dit_la_seule_voie_et_pas_pose_ta_cle(monte, monkeypatch):
    """Le refus générique propose « pose ta propre clé » — le geste que ce connecteur
    refuse. Il est remplacé par la voie qui existe : la clé du tenant."""
    from mcp.types import ErrorData
    from oto_mcp.access.resolve import CredentialUnavailable
    m, _, _, _ = monte

    def rien(provider, want="auto", **kw):
        raise CredentialUnavailable(ErrorData(code=-32602, message="Pose ta propre clé"))

    monkeypatch.setattr("oto_mcp.access.resolve_credential", rien)
    with pytest.raises(CredentialUnavailable) as e:
        _fn(m, "jev_ask")({"a": "b"}, {"q": NOUL})
    msg = str(e.value)
    assert "TENANT" in msg and "Pose ta propre clé" not in msg


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


def test_le_lot_verifie_le_quota_pour_toutes_ses_decisions(monte):
    m, _, _, releve = monte
    _fn(m, "jev_items")([{"n": i} for i in range(7)], {"q": NOUL})
    assert releve["units"] == 7


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

    def decide(state, questions, model=None, **kw):
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


def test_un_402_au_milieu_du_lot_releve_ce_qui_est_deja_paye(monte):
    """⚠️ LA régression : le refus remontait par `ex.map`, et le relevé n'était jamais
    atteint — trente décisions payées, un relevé vide, et un message qui disait
    « rien n'est facturé »."""
    from oto.tools.common.errors import UpstreamHTTPError
    m, client, _, releve = monte

    def decide(state, questions, model=None, **kw):
        if state["n"] >= 3:
            raise UpstreamHTTPError(402, {"message": "no credits"}, service="jev")
        return _reponse(cost=1.0e-05)

    client.decide.side_effect = decide
    with pytest.raises(McpError) as e:
        _fn(m, "jev_items")([{"n": i} for i in range(6)], {"q": NOUL}, parallel=1)
    assert releve["quantity"] == 30  # 3 décisions payées, relevées
    assert "3 réponse(s) déjà rendue(s) et relevée(s)" in str(e.value)
    assert "rien n'est facturé" not in str(e.value)


def test_une_exception_inattendue_d_un_fil_releve_puis_remonte(monte):
    m, client, _, releve = monte

    def decide(state, questions, model=None, **kw):
        if state["n"] == 1:
            raise KeyError("corps illisible")
        return _reponse(cost=1.0e-05)

    client.decide.side_effect = decide
    with pytest.raises(KeyError):
        _fn(m, "jev_items")([{"n": 0}, {"n": 1}], {"q": NOUL}, parallel=1)
    assert releve["quantity"] == 10


def test_passe_la_fenetre_les_etats_non_partis_sont_a_rejouer(monte, monkeypatch):
    m, client, _, _ = monte
    monkeypatch.setattr(jev, "LOT_FENETRE_S", 0.2)

    def decide(state, questions, model=None, **kw):
        time.sleep(0.15)
        return _reponse()

    client.decide.side_effect = decide
    r = _fn(m, "jev_items")([{"n": i} for i in range(5)], {"q": NOUL}, parallel=1)
    assert r["usage"]["decided"] == 2
    assert r["retry"] == [2, 3, 4]
    assert r["failed"] == 3 and all("send it again" in x["error"]
                                    for x in r["answers"] if "error" in x)


def test_la_fenetre_tient_sous_le_plafond_rest():
    # La pire décision partie au dernier instant (connexion 10 s + lecture) finit
    # avant le plafond de 45 s du chemin REST.
    assert jev.LOT_FENETRE_S + 10 + jev.LECTURE_S < jev.REST_CALL_LIMIT_S


def test_un_lot_complet_ne_demande_rien_a_rejouer(monte):
    m, _, _, _ = monte
    r = _fn(m, "jev_items")([{"n": 1}, {"n": 2}], {"q": NOUL})
    assert r["retry"] == []


def test_un_etat_trop_gros_est_refuse_avant_tout_appel(monte):
    m, client, _, _ = monte
    gros = {"doc": "x" * (jev.MAX_ETAT_OCTETS + 1)}
    with pytest.raises(McpError, match="octets"):
        _fn(m, "jev_ask")(gros, {"q": NOUL})
    with pytest.raises(McpError, match=r"items\[1\]"):
        _fn(m, "jev_items")([{"n": 1}, gros], {"q": NOUL})
    assert client.decide.call_count == 0


def test_parallel_mal_forme_est_une_erreur_nommee(monte):
    m, _, _, _ = monte
    with pytest.raises(McpError, match="parallel"):
        _fn(m, "jev_items")([{"n": 1}], {"q": NOUL}, parallel="beaucoup")


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
