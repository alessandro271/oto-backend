"""`engine=farm` au claim, et le refus qui NOMME la ferme (29/09/2026) — sans base : le
claim est doublé, on vérifie ce que la capacité transmet et ce qu'elle refuse. Le SQL
du routage est tenu par `test_routage_ferme_db.py`."""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import _modele
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


@pytest.fixture
def claims(monkeypatch):
    vus = []

    def claim(org_id, sub, **kw):
        vus.append(kw)
        return None

    monkeypatch.setattr(RJ.db, "claim_next_job", claim)
    monkeypatch.setattr(RJ.db, "repli_disponible", lambda *a, **kw: vus.append(kw))
    monkeypatch.setattr(RJ, "_produire_pour_une_campagne", lambda *a: None)
    return vus


def _claim(ctx, **kw):
    return RJ._jobs(ctx, RJ.JobsInput(op="claim", **kw))


_WORKER = ResolvedCtx(sub="otow-ferme", platform_worker=True)


def test_un_worker_de_ferme_le_dit_au_claim_et_au_repli(claims):
    _claim(_WORKER, provider="anthropic", org_key_only=True, engine="farm")
    assert claims and all(kw.get("ferme") is True for kw in claims)


def test_sans_engine_le_claim_est_celui_d_avant(claims):
    _claim(_WORKER, provider="anthropic")
    assert claims and all(kw.get("ferme") is False for kw in claims)


def test_engine_farm_est_refuse_a_un_porteur_au_jeton_d_org(claims):
    with pytest.raises(AuthzDenied) as e:
        _claim(ResolvedCtx(sub="membre", org_id=7), provider="anthropic", engine="farm")
    assert e.value.code == "farm_engine_platform_only" and not claims


def test_engine_farm_exige_la_famille_de_la_ferme(claims):
    with pytest.raises(AuthzDenied) as e:
        _claim(_WORKER, provider="mistral", engine="farm")
    assert e.value.code == "farm_engine_family" and not claims


def test_un_moteur_inconnu_ne_passe_pas_le_contrat():
    with pytest.raises(Exception):
        RJ.JobsInput(op="claim", engine="loop")


def test_le_refus_nomme_la_ferme_pour_une_org_routee():
    with pytest.raises(AuthzDenied) as e:
        _modele.exige_servi({"families": ["mistral"], "farm_routed": True}, "anthropic")
    assert e.value.code == "model_not_served"
    assert "ferme" in str(e.value.message)


def test_le_refus_reste_celui_d_avant_hors_ferme():
    with pytest.raises(AuthzDenied) as e:
        _modele.exige_servi({"families": ["mistral"], "farm_routed": False}, "anthropic")
    assert "ferme" not in str(e.value.message)
