"""Une cascade accordée à un travail hébergé couvre SES étapes — et rien d'autre.

Vécu : un agent dont la liste portait `waterfall_*` voyait chaque étape refusée
(`tool_not_in_job`), faute d'en lister l'outil de fournisseur. La liste dit ce que le MODÈLE
appelle ; une cascade accordée appelle pour lui les outils de sa table d'étapes, fixée dans
le code (`outils_du_travail.ouvrir_composite`, `waterfall/cites.outils_internes`).

Ici la cascade tourne pour de vrai jusqu'au VRAI `meta.executer_cible` — donc jusqu'à la
vraie garde `outils_du_travail.exiger` et au vrai appel de `activation_gate` — ; seuls sont
simulés l'outil du fournisseur (son `run`), la base (la charge du travail, l'activation lue)
et ce qu'un appel journalise ou règle.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import ErrorData, INVALID_PARAMS

from oto_mcp import access, call_axes, outils_du_travail, redaction, run_org, verrou_org
from oto_mcp.connectors import access_level_gate, activation_gate
from oto_mcp.connectors import health as connector_health
from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import meta
from oto_mcp.datastore import par_reference as pr
from oto_mcp.tools import waterfall as outils
from oto_mcp.tools import waterfall_lignes
from oto_mcp.waterfall import appel, moteur, ordre

LINKEDIN = "https://www.linkedin.com/in/jane-doe/"


#: Ce que répond un outil simulé (`{}` sinon) : une soumission asynchrone rend l'identifiant
#: que sa collecte relit, la collecte dit la réponse finie.
_REPONSES = {"lemlist_enrich": {"enrichment_id": "e1"},
             "lemlist_enrich_result": {"results": [{"done": True}]}}


class _Outil:
    """L'outil d'un fournisseur, à sa frontière : il relève l'appel et répond."""

    parameters: dict = {}

    def __init__(self, name: str, faits: list):
        self.name, self._faits = name, faits

    async def run(self, args):
        self._faits.append(self.name)
        return ToolResult(structured_content=dict(_REPONSES.get(self.name, {})))


@pytest.fixture
def harnais(monkeypatch):
    """Le travail 41 de l'agent 7, dont la liste est `etat["tools"]` ; les connecteurs
    inactifs pour l'org sont `etat["inactifs"]`. Rend l'état, avec les outils appelés."""
    etat = {"tools": ["waterfall_person"], "inactifs": set(), "faits": []}
    # Le travail porté par le jeton, et sa charge en base.
    monkeypatch.setattr(verrou_org, "travail_de_la_requete",
                        lambda: types.SimpleNamespace(job_id=41))
    monkeypatch.setattr(outils_du_travail.db, "charge_du_travail",
                        lambda job: {"tools": list(etat["tools"]), "trigger_id": 7})

    # Le catalogue : chaque nom résout un outil de fournisseur simulé.
    async def resoudre(_mcp, name):
        return _Outil(name, etat["faits"])
    monkeypatch.setattr(meta, "resoudre_outil", resoudre)

    # Ce qu'`executer_cible` lit en base ou journalise autour de la cible.
    monkeypatch.setattr(call_axes, "axes_for_call", lambda name: [])

    async def aucun_axe():
        return []
    monkeypatch.setattr(run_org, "pin_for_call", aucun_axe)
    # L'activation : la VRAIE `require_active`, sur la lecture de l'org simulée.
    monkeypatch.setattr(activation_gate, "_refus", lambda con: ErrorData(
        code=INVALID_PARAMS, message=f"`{con}` is not activated",
        data={"code": "connector_disabled"}) if con in etat["inactifs"] else None)

    async def couvert(*a, **k):
        return None
    monkeypatch.setattr(access_level_gate, "require_covered", couvert)
    monkeypatch.setattr(meta.budget, "ouvrir_appel", lambda *a, **k: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(access, "regler_appel", lambda *a, **k: None)
    monkeypatch.setattr(access, "regler_echec", lambda *a, **k: None)

    async def rien(*a, **k):
        return None
    monkeypatch.setattr(meta, "_trace_target_call", rien)
    monkeypatch.setattr(connector_health, "suivre_appel", rien)
    monkeypatch.setattr(redaction, "redact_result", lambda service, result: result)

    # La cascade : l'identité, l'ordre écrit, une clé pour chaque étape, aucune attente.
    monkeypatch.setattr(moteur, "current_user_sub_from_token", lambda: "sub-test")
    monkeypatch.setattr(ordre, "_configured_steps", lambda ns: None)
    monkeypatch.setattr(ordre, "_key_modes", lambda sub, tools: {t: "org" for t in tools})
    monkeypatch.setattr(appel, "POLL_FIRST_S", 0)
    monkeypatch.setattr(appel, "POLL_EVERY_S", 0)
    return etat


def _cascade(ns: str, outil_en_cours: str | None = None, **kwargs) -> dict:
    """Appelle l'outil `ns` comme le bord MCP le fait : l'outil en cours posé
    (`CallContextMiddleware` → `verrou_org.poser_outil`), puis sa fonction."""
    m = FastMCP("t")
    outils.register(m)
    fn = asyncio.run(m.get_tool(ns)).fn

    async def _go():
        jeton = verrou_org.poser_outil(outil_en_cours or ns)
        try:
            return await fn(**kwargs)
        finally:
            verrou_org.lever_outil(jeton)
    return asyncio.run(_go())


def _tentative(out: dict, cle: str) -> dict:
    return next(a for a in out["attempts"] if a["provider"] == cle)


# ── la cascade accordée fait tourner ses étapes ──────────────────────────────

def test_avec_la_seule_cascade_l_etape_lemlist_tourne(harnais):
    out = _cascade("waterfall_person", linkedin_url=LINKEDIN, providers=["lemlist_db"])
    assert harnais["faits"] == ["lemlist_database"]
    assert _tentative(out, "lemlist_db").get("code") != outils_du_travail.CODE


def test_une_etape_asynchrone_soumet_et_collecte_sous_la_cascade(harnais):
    """`lemlist_enrich` soumet, `lemlist_enrich_result` collecte : deux outils hors de la
    liste, tous deux couverts — la collecte en cours de cascade comprise."""
    _cascade("waterfall_person", linkedin_url=LINKEDIN, providers=["lemlist"])
    assert harnais["faits"][0] == "lemlist_enrich"
    assert "lemlist_enrich_result" in harnais["faits"]


def test_le_mode_tableau_couvre_ses_etapes_par_son_propre_nom(harnais):
    """`waterfall_person_act` dans la liste, la cascade lancée sous ce nom : couverte."""
    harnais["tools"] = ["waterfall_person_act"]
    _cascade("waterfall_person", outil_en_cours="waterfall_person_act",
             linkedin_url=LINKEDIN, providers=["lemlist_db"])
    assert harnais["faits"] == ["lemlist_database"]


def test_le_vrai_mode_tableau_couvre_ses_etapes_ligne_par_ligne(harnais, monkeypatch):
    """Le chemin vécu : `waterfall_company_act(op="update_rows")` seul dans la liste, une
    cascade par ligne (tâches parallèles de `fill_rows`), l'étape lemlist appelée par le
    vrai `executer_cible` pour chaque recherche."""
    harnais["tools"] = ["waterfall_company_act"]
    lignes = [{"_id": "r1", "website": "acme.com"}, {"_id": "r2", "website": "beta.io"}]
    store = types.SimpleNamespace(
        dernier_tableau={"ns_id": 42, "namespace": "t"}, patches=[],
        get_schema=lambda adresse: {"fields": [{"key": "website"}, {"key": "company"}]},
        patch_schema=lambda adresse, fields: None, count_rows=lambda *a, **k: 0)
    monkeypatch.setattr(pr, "ouvrir", lambda datastore, **k: pr.Lot(
        store, "t", lignes, [], {}, [{"field": "company_status", "op": "empty",
                                      "value": True}]))
    monkeypatch.setattr(pr, "colonnes_inconnues", lambda lot, cols: [])
    monkeypatch.setattr(pr, "ecrire", lambda lot, rid, patch: None)
    monkeypatch.setattr(pr, "tenue_ailleurs", lambda ligne: False)
    monkeypatch.setattr(pr.identite, "numero", lambda t: {"datastore": 42})
    m = FastMCP("t")
    waterfall_lignes.register(m)
    fn = asyncio.run(m.get_tool("waterfall_company_act")).fn

    async def _go():
        jeton = verrou_org.poser_outil("waterfall_company_act")
        try:
            return await fn(op="update_rows", datastore="42", inputs={"domain": "website"},
                            output={"name": "company"}, status_column="company_status",
                            providers=["lemlist"])
        finally:
            verrou_org.lever_outil(jeton)
    out = asyncio.run(_go())
    assert harnais["faits"] == ["lemlist_database", "lemlist_database"]
    assert "missing_tools" not in out and not out.get("skipped_tool_not_in_job")


# ── ce que la cascade n'ouvre pas ────────────────────────────────────────────

def test_le_modele_ne_peut_toujours_pas_appeler_l_etape_en_direct(harnais):
    """Le bord MCP (`CallContextMiddleware`) et `oto_call` (`executer_cible`) gardent la
    liste seule : `lemlist_database` n'y est pas, il est refusé."""
    async def _direct():
        jeton = verrou_org.poser_outil("lemlist_database")
        try:
            await outils_du_travail.exiger("lemlist_database")
        finally:
            verrou_org.lever_outil(jeton)
    with pytest.raises(McpError) as e:
        asyncio.run(_direct())
    assert e.value.error.data["code"] == outils_du_travail.CODE

    async def _par_oto_call():
        jeton = verrou_org.poser_outil("oto_call")
        try:
            return await meta.executer_cible(_Outil("lemlist_database", harnais["faits"]),
                                             "sub-test", "lemlist_database",
                                             "lemlist_database", {})
        finally:
            verrou_org.lever_outil(jeton)
    with pytest.raises(McpError) as e:
        asyncio.run(_par_oto_call())
    assert e.value.error.data["code"] == outils_du_travail.CODE
    assert harnais["faits"] == []


def test_l_accord_ne_survit_pas_a_la_cascade(harnais):
    """Dans la même tâche, juste après la cascade : l'étape est de nouveau refusée."""
    async def _apres():
        jeton = verrou_org.poser_outil("waterfall_person")
        try:
            with outils_du_travail.ouvrir_composite(
                    ("waterfall_person",), {"lemlist_database"}):
                assert outils_du_travail.refus("lemlist_database") is None
            return outils_du_travail.refus("lemlist_database")
        finally:
            verrou_org.lever_outil(jeton)
    assert asyncio.run(_apres()) is not None


def test_un_outil_qui_n_est_pas_une_etape_reste_refuse_pendant_la_cascade(harnais):
    async def _pendant():
        jeton = verrou_org.poser_outil("waterfall_person")
        try:
            with outils_du_travail.ouvrir_composite(
                    ("waterfall_person",), {"lemlist_database"}):
                return (outils_du_travail.refus("folk_record"),
                        outils_du_travail.refus("oto_call"))
        finally:
            verrou_org.lever_outil(jeton)
    assert all(m is not None for m in asyncio.run(_pendant()))


def test_sans_la_cascade_dans_la_liste_ses_etapes_restent_refusees(harnais):
    """La cascade lancée sans être dans la liste (le bord l'aurait refusée) : l'accord
    relit la liste, il ne suppose rien."""
    harnais["tools"] = ["data_rows"]
    out = _cascade("waterfall_person", linkedin_url=LINKEDIN, providers=["lemlist_db"])
    assert harnais["faits"] == []
    assert _tentative(out, "lemlist_db")["code"] == outils_du_travail.CODE


def test_sous_un_autre_outil_en_cours_rien_n_est_accorde(harnais):
    """L'accord suit l'outil EN COURS, posé au bord : une cascade lancée sous un autre nom
    (un outil qui ne serait pas elle) n'ouvre rien."""
    harnais["tools"] = ["waterfall_person", "data_rows"]
    out = _cascade("waterfall_person", outil_en_cours="data_rows",
                   linkedin_url=LINKEDIN, providers=["lemlist_db"])
    assert harnais["faits"] == []
    assert _tentative(out, "lemlist_db")["code"] == outils_du_travail.CODE


def test_un_connecteur_inactif_pour_l_org_reste_saute(harnais):
    """L'accord lève la seule liste du travail : l'activation du connecteur de l'étape
    tient toujours, et l'étape est sautée en le disant."""
    harnais["inactifs"] = {"lemlist"}
    out = _cascade("waterfall_person", linkedin_url=LINKEDIN, providers=["lemlist_db"])
    assert harnais["faits"] == []
    assert _tentative(out, "lemlist_db")["code"] == "connector_disabled"


def test_hors_travail_heberge_rien_ne_change(harnais, monkeypatch):
    monkeypatch.setattr(verrou_org, "travail_de_la_requete", lambda: None)
    harnais["tools"] = []
    _cascade("waterfall_person", linkedin_url=LINKEDIN, providers=["lemlist_db"])
    assert harnais["faits"] == ["lemlist_database"]


# ── la collecte tardive, elle, est un appel du modèle ────────────────────────

def test_une_reponse_pending_dit_l_outil_de_collecte_que_l_agent_n_a_pas(harnais,
                                                                         monkeypatch):
    """La cascade s'arrête sur une réponse `pending` : le MODÈLE la collectera plus tard,
    hors de la cascade. Hors de sa liste, le reçu le dit, avec le geste qui le donne."""
    async def jamais_finie(_mcp, sub, step, submitted, deadline):
        return None, {"id": "x"}, False
    monkeypatch.setattr(appel, "_poll", jamais_finie)
    harnais["tools"] = ["waterfall_email"]
    out = _cascade("waterfall_email", linkedin_url=LINKEDIN, providers=["fullenrich"])
    assert out["pending"][0]["tool"] == "fullenrich_result"
    assert out["missing_tools"] == ["fullenrich_result"]
    assert out["fix"] == {"tool": "oto_trigger", "op": "update", "trigger_id": 7,
                          "tools": ["waterfall_email", "fullenrich_result"]}
    harnais["tools"] = ["waterfall_email", "fullenrich_result"]
    out = _cascade("waterfall_email", linkedin_url=LINKEDIN, providers=["fullenrich"])
    assert out["pending"] and "missing_tools" not in out
