"""L'onglet Santé d'une org (`capabilities/agent_health.py`), en lecture, sur une vraie base.

Ce qu'on prouve : un problème par cause qui attend le client, avec les agents qu'il
arrête ; une panne de notre côté ne se dit qu'après six heures, à part, sans geste, et
son agent compte d'ici là comme fonctionnant ; un membre ne voit que les agents qu'il
voit ; ce que le médecin a réparé se lit avec sa phrase ; chaque item dit le connecteur
et la réponse du fournisseur, jamais notre texte interne ; une org archivée ne se lit pas.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from oto_mcp import alertes_slack_agents as A
from oto_mcp.capabilities import agent_health as H
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx

ORG = 8450


@pytest.fixture()
def base(live, monkeypatch):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM agent_episodes WHERE org_id = %s", (ORG,))
        conn.execute("DELETE FROM runner_triggers WHERE org_id = %s", (ORG,))
    monkeypatch.setenv("OTO_ALERTES_SLACK_AGENTS_ENABLED", "1")
    from oto_mcp import alerte_agents_bloques
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous", lambda org, qui: org == ORG)
    monkeypatch.setattr(A, "langue", lambda org: "fr")
    monkeypatch.setattr(A, "marque_de_l_org", lambda org: SimpleNamespace(nom="Acme", langue=""))


def _agent(label, sub="jane", enabled=True, model=None):
    from oto_mcp.db import runner_triggers as rt
    t = rt.create_trigger(ORG, sub, procedure="p", tz="UTC", tools=["a"], cron="0 6 * * *",
                          next_due="2999-01-01 00:00:00+00", label=label, model=model)
    if not enabled:
        from oto_mcp.db._conn import _connect
        with _connect() as conn:
            conn.execute("UPDATE runner_triggers SET enabled = false WHERE id = %s", (t["id"],))
    return t["id"]


def _panne(agent, cote="proprietaire", classe="cle_refusee", empreinte=None,
           motif="HTTP 401"):
    from oto_mcp.db import episodes_agents
    return episodes_agents.ouvrir_ou_rafraichir(
        org_id=ORG, trigger_id=agent, classe=classe, cote=cote, motif=motif,
        job_id=None, travaux=1, empreinte=empreinte)


def _lire(sub="jane"):
    return H._lire(ResolvedCtx(sub=sub), H.AgentHealthInput(org_id=ORG))


def test_un_probleme_par_cause_avec_les_agents_qu_il_arrete(base):
    a, b, c = _agent("Relances"), _agent("Veille"), _agent("Leads")
    _panne(a), _panne(b)
    _panne(c, cote="plateforme", classe="refus_passager")
    out = _lire()
    assert (out["watched"], out["working"]) == (3, 1), "notre panne se tait six heures"
    (pb,) = out["needs_you"]
    assert "a été refusé" in pb["headline"] and pb["action"] == "connectors"
    assert [x["label"] for x in pb["agents"]] == ["Relances", "Veille"]
    assert pb["since"] and out["covered"] is True
    assert out["on_us"] == [], "comme sur la carte de l'agent : rien avant six heures"
    assert set(out) == {"watched", "working", "needs_you", "on_us", "fixed", "covered"}


def test_un_membre_ne_voit_pas_l_agent_d_un_autre(base):
    _agent("Le mien")
    _panne(_agent("Le sien", sub="bob"))
    out = _lire()
    assert out["watched"] == 1 and out["needs_you"] == []


def test_un_agent_eteint_ne_compte_pas(base):
    _panne(_agent("Éteint", enabled=False))
    assert (_lire()["watched"], _lire()["needs_you"]) == (0, [])


def test_ce_que_le_medecin_a_repare_se_lit_avec_sa_phrase(base):
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    a, b = _agent("Relances"), _agent("Veille")
    ea, eb = _panne(a, cote="plateforme", classe="refus_passager"), _panne(b, cote="inconnu")
    episodes_agents.noter_reparation(ea["id"], action="relance", acteur="medecin", job_id=1)
    episodes_agents.noter_reparation(eb["id"], action="ajout_outil", acteur="medecin",
                                     detail="+ serper_search")
    rt.noter_reparation_pour_le_client(b, ORG, "Nous avons ajouté la recherche web.")
    for e in (ea, eb):
        episodes_agents.poser(int(e["id"]), statut="clos", resolution="gueri")
    fixed = {f["agent"]["label"]: f for f in _lire()["fixed"]}
    assert fixed["Relances"]["what"].startswith("Il s'est arrêté sur une panne passagère")
    assert fixed["Veille"]["what"] == "Nous avons ajouté la recherche web."
    assert set(fixed["Veille"]) == {"episode_id", "agent", "at", "what"}, \
        "la lecture ne propose aucun geste"


def test_une_panne_pas_encore_comprise_est_a_nous(base):
    _panne(_agent("Relances"), cote="inconnu", classe="inconnu")
    out = _lire()
    assert out["needs_you"] == [] and out["working"] == 0
    assert out["on_us"][0]["side"] == "operator"
    assert out["on_us"][0]["headline"] == H.A_NOUS_INCONNU["fr"]


def test_eteint_la_route_repond_404(base, monkeypatch):
    monkeypatch.delenv("OTO_ALERTES_SLACK_AGENTS_ENABLED")
    with pytest.raises(AuthzDenied) as e:
        _lire()
    assert (e.value.status, e.value.code) == (404, "agent_alerts_off")


def test_une_org_d_un_tenant_tiers_n_est_pas_suivie(base, monkeypatch):
    from oto_mcp import alerte_agents_bloques
    monkeypatch.setattr(alerte_agents_bloques, "org_a_nous", lambda org, qui: False)
    assert _lire()["covered"] is False


def test_une_panne_de_notre_cote_qui_dure_six_heures_se_dit_autrement(base):
    from oto_mcp.db._conn import _connect
    vieux, jeune, inconnu = _agent("Vieux"), _agent("Jeune"), _agent("Inconnu")
    ep = _panne(vieux, cote="plateforme", classe="refus_passager")
    _panne(jeune, cote="plateforme", classe="refus_passager")
    ei = _panne(inconnu, cote="inconnu", classe="inconnu")
    with _connect() as conn:
        conn.execute("UPDATE agent_episodes SET ouvert_at = NOW() - interval '7 hours' "
                     "WHERE id = ANY(%s)", ([ep["id"], ei["id"]],))
    operateur, plateforme = sorted(_lire()["on_us"], key=lambda p: p["side"])
    assert plateforme["lasting"] is True
    assert "plus de six heures" in plateforme["headline"]
    assert [a["label"] for a in plateforme["agents"]] == ["Vieux"], "la jeune se tait"
    assert "rien à faire du vôtre" in plateforme["headline"]
    assert operateur["lasting"] is False, "la plateforme seulement"
    assert "six heures" not in operateur["headline"]


def test_chaque_item_dit_le_connecteur_et_la_reponse_du_fournisseur(base):
    """Le connecteur que le motif nomme — ou, pour une clé de modèle refusée, le
    fournisseur du modèle de l'agent — et la réponse du fournisseur passée par la liste
    blanche ; jamais notre texte interne. `connectors` est une liste (vide sinon),
    `error` un texte ou null."""
    a = _agent("Relances", model="claude-sonnet-5")
    b = _agent("Veille", model="claude-sonnet-5")
    _panne(a, motif="fin_anormale (authentication_failed, HTTP 401 : Invalid API key)")
    _panne(b, cote="inconnu", classe="inconnu",
           motif="RuntimeError : run_start : un texte interne `x_y`")
    out = _lire()
    (pb,) = out["needs_you"]
    assert pb["connectors"] == ["Anthropic"], "le fournisseur du modèle, en repli"
    assert pb["error"] == "HTTP 401 : Invalid API key"
    (nous,) = out["on_us"]
    assert nous["connectors"] == [] and nous["error"] is None, "notre texte ne sort pas"


# ── La route se lit dans une org vivante (`ORG_MEMBER_OF_LIVE`) ───────────────────

def _authz():
    from oto_mcp.capabilities.registry import CAPABILITIES
    (cap,) = [c for c in CAPABILITIES if c.key == "org.agent_health.get"]
    return cap.authz


def test_une_org_archivee_ne_se_lit_pas(monkeypatch):
    from oto_mcp import access, org_store, roles
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "user")
    archivee = {"v": False}
    monkeypatch.setattr(org_store, "is_archived_org", lambda org: archivee["v"])
    inp = H.AgentHealthInput(org_id=ORG)
    assert _authz()(RawCtx(sub="jane"), inp).org_id == ORG
    archivee["v"] = True
    with pytest.raises(AuthzDenied) as e:
        _authz()(RawCtx(sub="jane"), inp)
    assert (e.value.status, e.value.code) == (409, "org_archived")


def test_un_non_membre_n_apprend_pas_que_l_org_est_archivee(monkeypatch):
    from oto_mcp import org_store, roles
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    monkeypatch.setattr(org_store, "is_archived_org", lambda org: True)
    with pytest.raises(AuthzDenied) as e:
        _authz()(RawCtx(sub="intrus"), H.AgentHealthInput(org_id=ORG))
    assert e.value.status == 403
