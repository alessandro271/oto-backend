"""Un outil renommé répond sous ses DEUX noms — l'ancien se dit déprécié, sans date (#519).

Lot B1 de #519 : le produit ne dit plus « doctrine », il dit **guide**. Renommer un
outil MCP casserait tout appelant qui vit hors de ce dépôt — dashboard, extension,
CLI, fronts partenaires, flotte d'agents, et toute la prose déjà écrite (procédures
d'org, guides, corps de guide) qui cite l'ancien nom. Alors on ne renomme pas : on
DOUBLE. Le nouveau nom naît, l'ancien continue de répondre, et il porte en tête de
sa description un avis de dépréciation SANS DATE : il s'en ira quand son usage compté
sera nul, par une PR qui datera alors le retrait (lot D, #526 ; décision du 23/11/2026).

Ce que ces tests gardent, dans l'ordre de ce qui coûterait le plus cher :

1. **L'ancien nom appelle le nouvel outil**, avec les mêmes arguments, à l'octet.
   C'est le seul invariant qui compte pour un appelant : sa requête d'hier aboutit.
2. **Le serveur ne voit JAMAIS l'ancien nom.** La traduction est faite au bord du
   protocole ; tout ce qui est en aval — gates de contexte d'appel, denylist de
   visibilité, journal `tool_calls`, refs `<tool:slug>` — continue de lire un seul
   nom pour un seul outil. Sans ça, un toggle posé sur l'ancien nom ne mordrait pas
   sur le nouveau, et le journal compterait un outil deux fois.
3. **Les deux entrées de `tools/list` sont identiques** hors le nom et l'avis :
   schéma d'entrée, schéma de sortie, annotations. Un alias qui dérive de son outil
   est un piège — l'agent lit un contrat et en appelle un autre.
4. **Aucune date de retrait n'est servie**, et une date de REVUE passée
   (`deprecations.REVUE*`) ne change rien à ce qui est servi : plus de couperet.
5. **La table ne ment pas** : chaque cible est un outil réellement monté, et aucun
   ancien nom ne recouvre un outil réel.
"""
from __future__ import annotations

import re

from _mcp_app import static_mcp as _test_mcp

import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from oto_mcp import deprecations, server, session_org
from oto_mcp.middleware.alias import ToolAliasMiddleware

_ARGS = {"op": "list", "org_id": 1}


async def _renvoie(valeur):
    return valeur


async def _liste_servie(monkeypatch, sub=None):
    """La liste telle qu'un client la reçoit — le VRAI middleware sur les VRAIS
    outils montés."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: sub)
    tools = await _test_mcp().list_tools(run_middleware=False)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    return await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie(tools))


async def _recu_en_aval(monkeypatch, demande, args, sub=None):
    """(nom, arguments) que la chaîne EN AVAL du middleware voit passer."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: sub)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name=demande, arguments=dict(args)),
        method="tools/call")
    vus = []

    async def _suivant(c):
        vus.append((c.message.name, dict(c.message.arguments or {})))
        return ToolResult(content=[mt.TextContent(type="text", text="réponse")])

    rendu = await ToolAliasMiddleware().on_call_tool(ctx, _suivant)
    assert rendu.content[-1].text == "réponse"
    return vus[0]


# ── 1 & 2. L'ancien nom appelle le nouvel outil, et lui seul ─────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("ancien,canonique", sorted(deprecations.TOOLS.items()))
async def test_lancien_nom_appelle_exactement_le_nouvel_outil(
        monkeypatch, ancien, canonique):
    """L'empreinte de ce qui atteint le serveur est la MÊME pour les deux noms."""
    par_lancien = await _recu_en_aval(monkeypatch, ancien, _ARGS)
    par_le_neuf = await _recu_en_aval(monkeypatch, canonique, _ARGS)
    assert par_lancien == par_le_neuf == (canonique, _ARGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("ancien", sorted(deprecations.TOOLS))
async def test_le_serveur_ne_connait_pas_lancien_nom(ancien):
    """L'alias vit au BORD du protocole, pas dans le registre : c'est ce qui garantit
    qu'aucun gate, aucune denylist et aucun journal n'a deux noms à tenir en phase."""
    noms = {t.name for t in await _test_mcp().list_tools(run_middleware=False)}
    assert ancien not in noms, (
        f"`{ancien}` est monté comme un VRAI outil. Un alias de dépréciation se sert "
        "au bord du protocole ; monté, il doublerait le journal, échapperait au "
        "toggle posé sur son canonique, et survivrait au lot D sans qu'on le voie.")


# ── 3. Les deux entrées servies décrivent le même contrat ───────────────────

@pytest.mark.asyncio
async def test_la_liste_sert_les_deux_noms(monkeypatch):
    noms = {t.name for t in await _liste_servie(monkeypatch)}
    for ancien, canonique in deprecations.TOOLS.items():
        assert canonique in noms, f"`{canonique}` n'est pas servi"
        assert ancien in noms, (
            f"`{ancien}` a disparu de `tools/list` avant sa date de retrait — un "
            "client qui ne lit que la liste ne saura jamais qu'il devait migrer.")


@pytest.mark.asyncio
async def test_lalias_decrit_le_meme_contrat_que_son_outil(monkeypatch):
    """Hors le nom, l'avis en tête et le titre (propre à chaque entrée, cf.
    `tests/test_tool_alias_titres.py`) : même schéma d'entrée, même schéma de sortie."""
    servis = {t.name: t for t in await _liste_servie(monkeypatch)}
    hors_contrat = {"name": True, "description": True, "title": True,
                    "annotations": {"title"}}
    for ancien, canonique in deprecations.TOOLS.items():
        a, c = servis[ancien], servis[canonique]
        assert (a.model_dump(exclude=hors_contrat)
                == c.model_dump(exclude=hors_contrat)), (
            f"`{ancien}` a dérivé de `{canonique}` : l'agent lirait un contrat et en "
            "appellerait un autre.")
        assert a.description == deprecations.avis(canonique) + (c.description or "")


@pytest.mark.asyncio
async def test_lavis_est_en_TETE_de_la_description(monkeypatch):
    """En tête, pas en queue : un client qui tronque à 400 caractères n'aurait averti
    personne, et c'est la première chose que le modèle lit pour choisir."""
    servis = {t.name: t for t in await _liste_servie(monkeypatch)}
    for ancien, canonique in deprecations.TOOLS.items():
        debut = servis[ancien].description[:120]
        assert debut.startswith("Déprécié : utilisez `" + canonique + "`")
        assert deprecations.PREAVIS_DE_RETRAIT in debut


# ── 4. Aucune date de retrait servie ; une date de revue ne change rien ─────
# Décision du 23/11/2026 : plus aucun couperet par date. Un ancien nom reste servi
# tant que son usage compté n'est pas nul ; la PR de retrait posera seule une date
# ferme, au moins `PREAVIS_MOIS` après le premier avis daté qu'elle servira.

_UNE_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}")


async def _textes_servis(monkeypatch) -> list:
    """Tous les textes d'avis que ce module fait servir : description et réponse de
    chaque ancien nom d'outil, avis d'une valeur réécrite, annonce du connecteur `api`,
    entrées dépréciées et schémas d'hier de l'OpenAPI, en-têtes des 308."""
    from starlette.requests import Request

    from oto_mcp import openapi
    from oto_mcp.api import alias_routes

    servis = {t.name: t for t in await _liste_servie(monkeypatch)}
    textes = [deprecations.avis(c) for c in deprecations.TOOLS.values()]
    # La description entière commence par l'avis (celle du canonique peut, elle,
    # citer des dates qui ne sont pas des retraits).
    assert all(servis[a].description.startswith(deprecations.avis(c))
               for a, c in deprecations.TOOLS.items())
    textes += [deprecations.avis_d_appel(a) for a in deprecations.TOOLS]
    for champ, (renommees, _revue) in deprecations.VALEURS.items():
        textes += [deprecations.valeur_canonique(champ, v)[1] for v in renommees]
    textes.append(deprecations.annonce_connecteur_api_du_jour(
        deprecations.ANNONCE_CONNECTEUR_API))
    doc = openapi.build()
    for alias in tuple(deprecations.REST) + tuple(deprecations.alias_rest_derives()):
        op = doc["paths"][alias.ancien][alias.verbe.lower()]
        textes += [op["summary"], op["description"], repr(op["responses"])]
        requete = Request({"type": "http", "method": alias.verbe, "path": alias.ancien,
                           "path_params": {}, "query_string": b"", "headers": []})
        r = await alias_routes._redirection(alias)(requete)
        assert "sunset" not in r.headers and r.headers["deprecation"] == "true"
        textes.append(repr(dict(r.headers)))
    textes += [doc["components"]["schemas"][a]["description"]
               for a in deprecations.SCHEMAS]
    return textes


@pytest.mark.asyncio
async def test_aucun_texte_servi_ne_porte_de_date_de_retrait(monkeypatch):
    for texte in await _textes_servis(monkeypatch):
        assert not _UNE_DATE.search(texte), f"date servie dans un avis : {texte!r}"
        assert "Sunset" not in texte and "retrait le" not in texte, texte


@pytest.mark.asyncio
async def test_une_date_de_revue_passee_ne_change_rien_a_ce_qui_est_servi(monkeypatch):
    """Une date de revue dit quand relire les compteurs, jamais quand l'alias s'en va :
    passée, elle laisse chaque alias servi, avec le même avis, à l'octet."""
    import datetime
    avant = await _textes_servis(monkeypatch)
    passee = datetime.date(2000, 1, 1)
    for nom in ("REVUE", "REVUE_CONNECTEUR_API", "REVUE_DATASTORE", "REVUE_PROCEDURE"):
        monkeypatch.setattr(deprecations, nom, passee)
    monkeypatch.setattr(deprecations, "_REVUE_DES_OUTILS",
                        {a: passee for a in deprecations._REVUE_DES_OUTILS})
    monkeypatch.setattr(deprecations, "VALEURS",
                        {c: (r, passee) for c, (r, _) in deprecations.VALEURS.items()})
    assert all(a.revue == passee for a in deprecations._alias_datastore())
    assert await _textes_servis(monkeypatch) == avant
    noms = {t.name for t in await _liste_servie(monkeypatch)}
    assert set(deprecations.TOOLS) <= noms, "un alias a disparu à sa date de revue"
    assert deprecations.tool_canonique("http_get") == "api_get"


# ── 5. La table ne ment pas ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chaque_cible_est_un_outil_reellement_monte():
    noms = {t.name for t in await _test_mcp().list_tools(run_middleware=False)}
    orphelines = sorted({c for c in deprecations.TOOLS.values() if c not in noms})
    assert not orphelines, (
        f"{orphelines} : cibles d'un alias déprécié, absentes du registre. L'ancien "
        "nom serait alors servi et injoignable — pire qu'un retrait sec.")


@pytest.mark.asyncio
async def test_aucun_ancien_nom_ne_recouvre_un_outil_reel(monkeypatch):
    """Ceinture ET bretelles : une collision ferait éclipser un outil par un alias,
    sans que rien ne le dise. La liste servie garde le nom RÉEL."""
    vrais = [t for t in await _test_mcp().list_tools(run_middleware=False)
             if t.name in deprecations.TOOLS.values()]
    assert vrais, "aucun outil déprécié monté — table vide ou cible écorchée"
    usurpateur = vrais[0].model_copy(update={"name": next(iter(deprecations.TOOLS))})
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: None)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    rendus = await ToolAliasMiddleware().on_list_tools(
        ctx, lambda _c: _renvoie(vrais + [usurpateur]))
    noms = [t.name for t in rendus]
    assert noms.count(usurpateur.name) == 1, "un alias a écrasé un nom déjà pris"


@pytest.mark.asyncio
async def test_un_outil_absent_de_la_liste_ne_revient_pas_par_son_ancien_nom(
        monkeypatch):
    """L'alias est dérivé de la liste RÉELLEMENT servie : il hérite du filtrage de
    visibilité. Un outil masqué pour ce compte ne doit pas réapparaître par la porte
    de derrière — ce serait un contournement de la denylist, pas une compatibilité."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: None)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    rendus = await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie([]))
    assert rendus == []


# ── 6. Le préavis tient l'engagement CONTRACTUEL ────────────────────────────
# L'Art 8.2 du contrat de service promet un préavis de deux mois avant toute rupture
# d'interface (référence et constat d'écart : #767). Ces deux tests existent pour qu'on ne
# puisse pas raccourcir ce préavis sans savoir ce qu'on touche — il a valu 30 jours du
# 29/08 au 01/09/2026 (#767), et personne ne l'avait décidé contre l'article. Il ne
# produit plus de date servie : c'est la PR de retrait qui la posera, au moins
# `PREAVIS_MOIS` après le premier avis daté qu'elle servira.


def test_le_preavis_annonce_tient_les_deux_mois_ecrits_au_contrat():
    """PLANCHER, pas égalité : allonger un préavis est toujours favorable à celui à
    qui il est dû ; descendre sous deux mois est un manquement, pas un réglage."""
    assert deprecations.PREAVIS_MOIS >= 2, (
        f"Le préavis servi est de {deprecations.PREAVIS_MOIS} mois. L'Art 8.2 en "
        "promet DEUX avant toute rupture d'interface : ce chiffre n'est pas un "
        "réglage d'opportunité, c'est ce qui a été écrit au client. Pour le "
        "raccourcir, il faut d'abord amender l'engagement.")


def test_le_preavis_se_compte_en_mois_CALENDAIRES_jamais_en_jours():
    """Deux mois ≠ 60 jours. L'approximation en jours tombe un jour trop tôt neuf
    fois sur douze, et toujours du même côté : contre le consommateur. C'est la
    famille d'écart de #767/#768 — un jour pris sans que personne l'ait décidé."""
    import datetime
    d = deprecations._plus_de_mois
    assert d(datetime.date(2026, 8, 29), 2) == datetime.date(2026, 10, 29)
    # ...et l'approximation, elle, aurait rendu la veille.
    assert datetime.date(2026, 8, 29) + datetime.timedelta(days=60) \
        == datetime.date(2026, 10, 28)
    # fin de mois : on retombe sur le dernier jour existant, jamais sur un 31/02.
    assert d(datetime.date(2026, 12, 31), 2) == datetime.date(2027, 2, 28)
    assert d(datetime.date(2026, 11, 30), 2) == datetime.date(2027, 1, 30)


# ── 7. La RÉPONSE d'un ancien nom porte l'avis, et le journal compte l'alias ─

async def _rendu(monkeypatch, demande):
    """(nom vu en aval, ancien nom lu en aval, réponse rendue) d'un appel par `demande`."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: None)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name=demande, arguments=dict(_ARGS)),
        method="tools/call")
    vus = []

    async def _suivant(c):
        vus.append((c.message.name, session_org.nom_deprecie_de_l_appel()))
        return ToolResult(content=[mt.TextContent(type="text", text="réponse")])

    rendu = await ToolAliasMiddleware().on_call_tool(ctx, _suivant)
    return vus[0], rendu


@pytest.mark.asyncio
@pytest.mark.parametrize("ancien,canonique", sorted(deprecations.TOOLS.items()))
async def test_la_reponse_d_un_ancien_nom_porte_l_avis_en_tete(monkeypatch, ancien,
                                                               canonique):
    (vu, nom_lu), rendu = await _rendu(monkeypatch, ancien)
    assert (vu, nom_lu) == (canonique, ancien)
    textes = [c.text for c in rendu.content]
    assert textes[-1] == "réponse", "la réponse de l'outil est rendue telle quelle"
    assert textes[0] == deprecations.avis_d_appel(ancien)
    assert f"`{ancien}` est renommé `{canonique}`" in textes[0]
    assert deprecations.PREAVIS_DE_RETRAIT in textes[0]
    assert session_org.nom_deprecie_de_l_appel() is None, "l'ancien nom ne fuit pas"


@pytest.mark.asyncio
async def test_la_reponse_du_nom_d_aujourd_hui_ne_porte_aucun_avis(monkeypatch):
    (vu, nom_lu), rendu = await _rendu(monkeypatch, "api_get")
    assert (vu, nom_lu) == ("api_get", None)
    assert [c.text for c in rendu.content] == ["réponse"]


def test_le_journal_compte_l_appel_par_un_ancien_nom():
    """`tool` reste le nom d'aujourd'hui (un seul nom pour un seul outil) ; l'usage de
    l'alias se lit dans `args.deprecated_name` — c'est ce qui dit quand le retirer."""
    from oto_mcp.calllog import apply_call_trace
    jeton = session_org.set_nom_deprecie("http_get")
    try:
        row = apply_call_trace({"tool": "api_get", "args": {"path": "/x"}}, None, ())
    finally:
        session_org.reset_nom_deprecie(jeton)
    assert row == {"tool": "api_get", "args": {"path": "/x", "deprecated_name": "http_get"}}
    assert apply_call_trace({"tool": "api_get", "args": {}}, None, ()) == {
        "tool": "api_get", "args": {}}


def test_les_anciens_noms_du_connecteur_api_ont_leur_propre_date_de_revue():
    assert all(deprecations.date_de_revue(a) == deprecations.REVUE_CONNECTEUR_API
               for a in deprecations.OUTILS_CONNECTEUR_API)
    assert deprecations.date_de_revue("oto_admin_doctrine") == deprecations.REVUE


# ── 8. Une VALEUR dépréciée réécrite est comptée, sur les deux faces ─────────

def _entree_d_hier():
    from oto_mcp.capabilities.resources import ResourceInput
    return ResourceInput.model_validate({"op": "list", "resource_type": "doctrine"})


def test_face_mcp_la_valeur_d_hier_rejoint_args_deprecated_value():
    """`resource_type=doctrine` réécrit en `procedure` : la ligne MCP porte
    `args.deprecated_value`, par le relevé de l'appel (`apply_call_trace`)."""
    from oto_mcp.calllog import apply_call_trace
    trace: dict = {}
    jeton = session_org.set_call_trace(trace)
    try:
        inp = _entree_d_hier()
    finally:
        session_org.reset_call_trace(jeton)
    assert inp.resource_type == "procedure"
    row = apply_call_trace({"tool": "oto_resource", "args": {"op": "list"}}, trace,
                           server._TRACED_ARGS)
    assert row["args"] == {"op": "list", "deprecated_value": "resource_type=doctrine"}


def test_face_rest_la_valeur_d_hier_rejoint_la_ligne_de_route(monkeypatch):
    """Même compteur sur la face REST : la ligne de `RestCallLogger` le porte."""
    import dataclasses

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.testclient import TestClient

    from oto_mcp.api import base as api_base
    from oto_mcp.api import routes as ar
    from oto_mcp.capabilities import _rest_adapter
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.registry import CAPABILITIES

    lignes: list = []

    def _emet(row):
        lignes.append(dict(row))

        async def _fait():
            return None
        return _fait()

    monkeypatch.setattr(ar, "_emit_rest_event", _emet)

    async def authentifie(request, verifier, **kw):
        return "u-1", None

    (cap,) = [c for c in CAPABILITIES if c.key == "resources.govern"]
    cap = dataclasses.replace(cap, authz=lambda raw, inp: ResolvedCtx(sub=raw.sub,
                                                                      org_id=None),
                              handler=lambda ctx, inp: {"resource_type": inp.resource_type})
    routes = _rest_adapter.make_routes(None, authentifie, api_base._json,
                                       api_base._json_error, api_base.options_handler,
                                       [cap])
    client = TestClient(Starlette(routes=routes,
                                  middleware=[Middleware(ar.RestCallLogger)]))
    r = client.post("/api/resources", json={"op": "list", "resource_type": "doctrine"})
    assert r.status_code == 200, r.text
    (ligne,) = lignes
    assert ligne["args"]["deprecated_value"] == "resource_type=doctrine"
    # Hors réécriture, rien.
    lignes.clear()
    client.post("/api/resources", json={"op": "list", "resource_type": "procedure"})
    assert "deprecated_value" not in (lignes[0]["args"] or {})
