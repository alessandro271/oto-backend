"""Les anciens noms du renommage « guide » sont RETIRÉS (lot D, #526, 23/11/2026).

Leurs compteurs étaient à zéro sur toutes les instances, et l'avis servi depuis le lot
B valait préavis (décision du 23/11/2026). Ce fichier garde qu'aucun ne revient en
douce :

1. l'ancien nom d'outil MCP est inconnu — ni outil, ni traduction ;
2. les anciens chemins REST rendent 404 — ni route, ni 308, ni entrée OpenAPI ;
3. l'ancienne valeur de `resource_type` est refusée comme toute valeur invalide.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from _mcp_app import static_mcp as _test_mcp

from oto_mcp import deprecations, openapi
from oto_mcp.api import routes as api_routes

ANCIEN_OUTIL = "oto_admin_" + "doc" + "trine"
ANCIEN_PREFIXE = "/api/me/" + "doc" + "trines"
ANCIENNE_VALEUR = "doc" + "trine"


class _FauxVerifieur:
    """`make_routes` ne fait que CAPTURER le verifier — jamais appelé au montage."""


# ── 1. L'outil ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_l_ancien_nom_d_outil_est_inconnu():
    assert ANCIEN_OUTIL not in deprecations.TOOLS
    outils = {t.name for t in await _test_mcp().list_tools()}
    assert ANCIEN_OUTIL not in outils and "oto_admin_guide" in outils


# ── 2. Les chemins REST ──────────────────────────────────────────────────────

@pytest.mark.parametrize("verbe, suite", [
    ("GET", "/library"), ("GET", "/library/un-slug"), ("DELETE", "/library/42"),
    ("POST", "/publish"), ("POST", "/fork"), ("GET", "/42"),
])
def test_les_anciens_chemins_rendent_404(verbe, suite):
    client = TestClient(Starlette(routes=api_routes.make_routes(_FauxVerifieur(),
                                                                mcp_instance=None)))
    r = client.request(verbe, ANCIEN_PREFIXE + suite, follow_redirects=False)
    assert r.status_code == 404, f"{verbe} {ANCIEN_PREFIXE}{suite} répond {r.status_code}"
    assert "deprecation" not in r.headers


def test_aucun_alias_ni_entree_openapi_ne_porte_l_ancien_prefixe():
    assert not [a for a in deprecations.alias_rest_derives()
                if a.ancien.startswith(ANCIEN_PREFIXE)]
    assert not [p for p in openapi.build()["paths"] if p.startswith(ANCIEN_PREFIXE)]


# ── 3. La valeur de `resource_type` ──────────────────────────────────────────

def _cap(key: str):
    from oto_mcp.capabilities.registry import CAPABILITIES
    return next(c for c in CAPABILITIES if c.key == key)


def test_l_ancienne_valeur_n_est_plus_reecrite():
    assert deprecations.VALEURS == {}


def test_la_surface_stricte_refuse_l_ancienne_valeur():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _cap("resources.govern.v2").Input(op="get", resource_type=ANCIENNE_VALEUR,
                                          resource_id="1")


def test_la_surface_heritee_refuse_l_ancienne_valeur_par_un_refus_nomme():
    from oto_mcp.capabilities._types import AuthzDenied, RawCtx
    cap = _cap("resources.govern")
    inp = cap.Input(op="get", resource_type=ANCIENNE_VALEUR, resource_id="1")
    assert inp.resource_type == ANCIENNE_VALEUR, "plus aucune réécriture"
    with pytest.raises(AuthzDenied) as refus:
        cap.authz(RawCtx(sub="u1"), inp)
    assert refus.value.code == "unsupported_resource_type"
