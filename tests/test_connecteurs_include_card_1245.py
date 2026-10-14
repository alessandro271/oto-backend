"""`GET /api/me/connectors?include=card` : le détail de tous les connecteurs en UN appel
(oto-backend#1245).

Une page Connecteurs lisait la carte verbeuse de chaque connecteur une à une
(`?verbose=true&name=<x>`, ~420 ms la carte, trois à la fois) : la même rafale que
`/api/me/projects` avant `op=list include=[...]`. La lecture d'ensemble existante
(`verbose=true`) porte la carte COMPLÈTE, dont 72 % de mode d'emploi markdown
(`doc_sections`) : ~745 Ko pour 145 connecteurs. `include=card` sert la ligne compacte
plus la carte SANS ce mode d'emploi ; `include=card,docs` la carte complète.

Ce que ce fichier tient :
1. l'entrée : virgule ou forme répétée, valeur inconnue refusée, `verbose` + `include`
   refusés (l'un serait ignoré en silence) — sans base ;
2. la projection : `card` = la carte moins `doc_sections`, `card` + `docs` = la ligne
   verbeuse, octet pour octet — sans base ;
3. sur la vraie route, base comprise : chaque ligne `include=card` est EXACTEMENT la
   ligne verbeuse moins `doc_sections` (`connect.callback_url`/`app_ready` compris), et
   les champs de carte d'un connecteur sont ceux de sa lecture ciblée ;
4. le nombre de requêtes de `include=card` est celui de la liste compacte, et ne suit
   pas le nombre de connecteurs (une lecture ciblée par carte en coûtait ~47 chacune).
"""
from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from oto_mcp.capabilities.connectors import selection as S


# ── 1. L'entrée ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("brut, attendu", [
    ("card", ["card"]), ("card,docs", ["card", "docs"]), (" card , docs ", ["card", "docs"]),
    (["card", "docs"], ["card", "docs"]), (["card,docs"], ["card", "docs"])])
def test_include_se_dit_dans_une_url(brut, attendu):
    assert S.MyConnectorsInput(include=brut).include == attendu


@pytest.mark.parametrize("brut", ["detail", "card,doc", ["card", "auth"]])
def test_une_valeur_inconnue_est_refusee_jamais_ignoree(brut):
    with pytest.raises(ValidationError, match="unknown value"):
        S.MyConnectorsInput(include=brut)


def test_verbose_et_include_ensemble_sont_refuses():
    with pytest.raises(ValidationError, match="not both"):
        S.MyConnectorsInput(verbose=True, include="card")


# ── 2. La projection ────────────────────────────────────────────────────────────────

@pytest.fixture
def catalogue(monkeypatch):
    # Les modes d'emploi résolvent l'URL publique du serveur (lien de consentement).
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://oto.example")
    from oto_mcp import providers
    return providers.public_catalog()


def test_card_est_la_carte_sans_le_mode_d_emploi(catalogue):
    for c in catalogue:
        ligne = S._projection(c, S.MyConnectorsInput(include="card"))
        assert "doc_sections" not in ligne, c["name"]
        assert ligne == {k: v for k, v in c.items() if k != "doc_sections"}, c["name"]


def test_card_et_docs_est_la_ligne_verbeuse(catalogue):
    for c in catalogue:
        assert (S._projection(c, S.MyConnectorsInput(include="card,docs"))
                == S._projection(c, S.MyConnectorsInput(verbose=True))), c["name"]


def test_docs_seul_ajoute_le_mode_d_emploi_a_la_ligne_compacte(catalogue):
    c = next(c for c in catalogue if c.get("doc_sections"))
    ligne = S._projection(c, S.MyConnectorsInput(include="docs"))
    assert set(ligne) == set(S._COMPACT_KEYS) | {"doc_sections"}


def test_card_allege_vraiment(catalogue):
    """Le poids, pas une promesse : sans `doc_sections`, la liste pèse moins de la
    moitié de la carte complète (un tiers au relevé du 14/10/2026)."""
    card = len(json.dumps([S._projection(c, S.MyConnectorsInput(include="card"))
                           for c in catalogue]))
    verbeux = len(json.dumps(catalogue))
    assert card < verbeux / 2, (card, verbeux)


# ── 3 et 4. La vraie route, base comprise ───────────────────────────────────────────

@pytest.fixture(scope="module")
def jeu(live):
    import _jeu_connecteurs_1148 as jeu_1148
    import _mcp_app
    _mcp_app.static_mcp()      # le registre du SERVEUR : flux de connexion compris
    avant = {k: os.environ.get(k) for k in ("OTO_MCP_MASTER_KEY", "OTO_MCP_PUBLIC_URL")}
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
    os.environ.setdefault("OTO_MCP_PUBLIC_URL", "https://oto.example")
    try:
        yield jeu_1148.peupler()
    finally:
        for k, v in avant.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _ctx(jeu):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=jeu.sub, org_id=jeu.org)


def _compter(f):
    from test_connecteurs_me_rapide_1148 import _compter as compter
    return compter(f)


def test_chaque_ligne_card_est_la_ligne_verbeuse_sans_mode_d_emploi(jeu):
    ctx = _ctx(jeu)
    card = S._me(ctx, S.MyConnectorsInput(include="card"))
    verbeux = S._me(ctx, S.MyConnectorsInput(verbose=True))
    assert card["include"] == ["card"] and card["verbose"] is False
    assert len(card["connectors"]) == len(verbeux["connectors"]) > 50
    for a, b in zip(card["connectors"], verbeux["connectors"]):
        attendu = {k: v for k, v in b.items() if k != "doc_sections"}
        assert json.dumps(a, default=str) == json.dumps(attendu, default=str), a["name"]
    # Le flux de connexion garde sa partie propre au demandeur, comme en verbeux.
    avec_flux = [r for r in card["connectors"] if r.get("connect")]
    assert avec_flux and all("callback_url" in r["connect"] for r in avec_flux)


def test_la_carte_d_un_connecteur_est_celle_de_sa_lecture_ciblee(jeu):
    ctx = _ctx(jeu)
    lignes = {r["name"]: r for r in S._me(ctx, S.MyConnectorsInput(include="card"))["connectors"]}
    nom = next(n for n, r in lignes.items() if r.get("connect") and r.get("credential_fields"))
    ciblee = S._me(ctx, S.MyConnectorsInput(verbose=True, name=nom))["connectors"][0]
    # La lecture ciblée ajoute l'aptitude (`ready`…), que la liste ne calcule pas.
    for k in ("ready", "not_ready", "next_step", "doc_sections"):
        ciblee.pop(k, None)
    assert json.dumps(lignes[nom], default=str) == json.dumps(ciblee, default=str)


def test_card_coute_ce_que_coute_la_liste_compacte(jeu):
    ctx = _ctx(jeu)
    _, compact = _compter(lambda: S._me(ctx, S.MyConnectorsInput()))
    out, card = _compter(lambda: S._me(ctx, S.MyConnectorsInput(include="card")))
    _, une_carte = _compter(lambda: S._me(
        ctx, S.MyConnectorsInput(verbose=True, name=out["connectors"][0]["name"])))
    # Une requête de plus au plus : `app_ready` des flux de connexion, déjà payé en
    # verbeux. Ce qui ne doit jamais revenir : un coût par connecteur.
    assert card <= compact + 2, (compact, card)
    assert card < une_carte * 3, (
        f"toute la liste ({card} requêtes) devrait coûter moins que trois cartes "
        f"lues une à une ({une_carte} chacune)")
