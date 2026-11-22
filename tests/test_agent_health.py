"""L'onglet Santé d'une org (`capabilities/agent_health.py`), en lecture, sur une vraie base.

Ce qu'on prouve : un problème par cause qui attend le client, avec les agents qu'il
arrête ; une panne de notre côté ne se dit qu'après six heures, à part, sans geste, et
son agent compte d'ici là comme fonctionnant ; un membre ne voit que les agents qu'il
voit ; ce que le médecin a réparé se lit avec sa phrase ; chaque item dit le connecteur
et la réponse du fournisseur, jamais notre texte interne ; une org archivée ne se lit ni
ne s'écrit (409).

Et l'écriture : la réparation automatique naît coupée, un admin l'allume, rien ne s'écrit
quand l'instance a éteint les alertes ; défaire une réparation retire l'outil ajouté s'il
y est encore et que personne n'a écrit l'agent depuis, le garde sinon, en une transaction
qui tient deux clics simultanés.
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
        conn.execute("DELETE FROM agent_health_settings WHERE org_id = %s", (ORG,))
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
    assert set(out) == {"watched", "working", "needs_you", "on_us", "fixed", "auto_repair",
                        "covered"}


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
    assert set(fixed["Veille"]) == {"episode_id", "agent", "at", "what", "undo", "undone"}
    assert (fixed["Veille"]["undo"], fixed["Veille"]["undone"]) == (True, False), \
        "un ajout d'outil se défait, par qui peut modifier l'agent"
    assert fixed["Relances"]["undo"] is False, "une relance ne se défait pas"


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

def _authz(cle="org.agent_health.get"):
    from oto_mcp.capabilities.registry import CAPABILITIES
    (cap,) = [c for c in CAPABILITIES if c.key == cle]
    return cap.authz


_ENTREES = {"org.agent_health.get": H.AgentHealthInput(org_id=ORG),
            "org.agent_health.set": H.AgentHealthSaveInput(org_id=ORG, auto_repair=True),
            "org.agent_health.undo": H.AgentHealthUndoInput(org_id=ORG, episode_id=1)}


@pytest.mark.parametrize("cle", sorted(_ENTREES))
def test_une_org_archivee_ne_se_lit_ni_ne_s_ecrit(monkeypatch, cle):
    from oto_mcp import access, org_store, roles
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "user")
    archivee = {"v": False}
    monkeypatch.setattr(org_store, "is_archived_org", lambda org: archivee["v"])
    inp = _ENTREES[cle]
    assert _authz(cle)(RawCtx(sub="jane"), inp).org_id == ORG
    archivee["v"] = True
    with pytest.raises(AuthzDenied) as e:
        _authz(cle)(RawCtx(sub="jane"), inp)
    assert (e.value.status, e.value.code) == (409, "org_archived")


def test_seul_un_admin_choisit_la_reparation(live, monkeypatch):
    from oto_mcp import access, org_store, roles
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)
    monkeypatch.setattr(access, "get_user_role", lambda sub: "user")
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    monkeypatch.setattr(org_store, "is_archived_org", lambda org: False)
    with pytest.raises(AuthzDenied) as e:
        _authz("org.agent_health.set")(RawCtx(sub="jane"), _ENTREES["org.agent_health.set"])
    assert e.value.status == 403


def test_un_non_membre_n_apprend_pas_que_l_org_est_archivee(monkeypatch):
    from oto_mcp import org_store, roles
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    monkeypatch.setattr(org_store, "is_archived_org", lambda org: True)
    with pytest.raises(AuthzDenied) as e:
        _authz()(RawCtx(sub="intrus"), H.AgentHealthInput(org_id=ORG))
    assert e.value.status == 403


# ── Le choix de l'org : PUT ────────────────────────────────────────────────────────

def _poser(valeur, sub="jane"):
    return H._poser(ResolvedCtx(sub=sub),
                    H.AgentHealthSaveInput(org_id=ORG, auto_repair=valeur))


def test_la_reparation_automatique_nait_coupee_et_un_admin_l_allume(base):
    from oto_mcp.db import sante_agents
    assert _lire()["auto_repair"] is False, "aucune ligne : coupée"
    assert _poser(True)["auto_repair"] is True
    assert sante_agents.auto_repair(ORG) is True
    assert _poser(False)["auto_repair"] is False


def test_instance_eteinte_le_put_n_ecrit_rien(base, monkeypatch):
    from oto_mcp.db._conn import _connect
    monkeypatch.delenv("OTO_ALERTES_SLACK_AGENTS_ENABLED")
    with pytest.raises(AuthzDenied) as e:
        _poser(True)
    assert (e.value.status, e.value.code) == (404, "agent_alerts_off")
    with _connect() as conn:
        assert conn.execute("SELECT 1 FROM agent_health_settings WHERE org_id = %s",
                            (ORG,)).fetchone() is None, "refusé AVANT d'écrire"


# ── Défaire une réparation : POST …/undo ────────────────────────────────────────────

OUTIL = "serper_search"


def _reparer(agent):
    """Le geste du médecin, tel que `add_tool` l'écrit : la trace avec l'outil, l'outil
    donné à l'agent, l'`updated_at` de cette écriture rangé sur la trace."""
    from oto_mcp import db
    from oto_mcp.db import episodes_agents
    ep = _panne(agent, cote="inconnu", classe="inconnu")
    episodes_agents.noter_reparation(ep["id"], action="ajout_outil", acteur="medecin",
                                     detail=f"+ {OUTIL}", outil=OUTIL)
    t = db.get_trigger(agent, ORG)
    db.update_trigger(agent, ORG, {"tools": list(t["tools"]) + [OUTIL]}, par="medecin")
    assert episodes_agents.horodater_l_ajout(ep["id"], OUTIL, trigger_id=agent, org_id=ORG,
                                             acteur="medecin")
    return ep


def _defaire(ep, sub="jane"):
    return H._defaire(ResolvedCtx(sub=sub),
                      H.AgentHealthUndoInput(org_id=ORG, episode_id=int(ep["id"])))


def test_defaire_retire_l_outil_et_le_medecin_ne_le_redonne_pas(base):
    from oto_mcp import db
    from oto_mcp.db import episodes_agents, runner_triggers as rt
    a = _agent("Veille")
    ep = _reparer(a)
    rt.noter_reparation_pour_le_client(a, ORG, "Nous avons ajouté la recherche web.")
    episodes_agents.poser(int(ep["id"]), statut="clos", resolution="gueri")
    out = _defaire(ep)
    assert out["undo_result"] == {"removed": [OUTIL], "kept": []}
    assert db.get_trigger(a, ORG)["tools"] == ["a"]
    assert db.get_trigger(a, ORG)["repair"] is None, "la note de cet épisode s'efface"
    assert OUTIL in rt.outils_annules(a, ORG)
    (f,) = out["fixed"]
    assert (f["undo"], f["undone"]) == (False, True)
    assert f["what"] == H._DEFAITE["fr"]
    gestes = episodes_agents.lire(int(ep["id"]))["reparations"]
    assert [g["action"] for g in gestes] == ["ajout_outil", "annulation"]
    assert gestes[-1]["acteur"] == "jane"
    with pytest.raises(AuthzDenied) as e:
        _defaire(ep)
    assert e.value.code == "repair_not_undoable", "une fois"


def test_un_outil_remis_a_la_main_est_garde(base):
    """Une personne a retiré l'outil puis l'a remis : l'agent a été écrit depuis la
    réparation, l'outil présent n'est plus sûrement celui du médecin — il reste."""
    from oto_mcp import db
    from oto_mcp.db import runner_triggers as rt
    a = _agent("Veille")
    ep = _reparer(a)
    db.update_trigger(a, ORG, {"tools": ["a"]}, par="jane")
    db.update_trigger(a, ORG, {"tools": ["a", OUTIL]}, par="jane")
    out = _defaire(ep)
    assert out["undo_result"] == {"removed": [], "kept": [OUTIL]}
    assert db.get_trigger(a, ORG)["tools"] == ["a", OUTIL]
    assert OUTIL in rt.outils_annules(a, ORG), "le médecin ne le redonne pas pour autant"


def test_un_outil_deja_retire_ne_se_retire_pas_deux_fois(base):
    from oto_mcp import db
    a = _agent("Veille")
    ep = _reparer(a)
    db.update_trigger(a, ORG, {"tools": ["a", "b"]}, par="jane")
    out = _defaire(ep)
    assert out["undo_result"] == {"removed": [], "kept": []}
    assert db.get_trigger(a, ORG)["tools"] == ["a", "b"], "l'écriture de jane tient"


def test_deux_annulations_simultanees_ne_font_qu_un_geste(base):
    import threading
    from oto_mcp import db
    from oto_mcp.db import episodes_agents, sante_agents
    a = _agent("Veille")
    ep = _reparer(a)
    depart, resultats = threading.Barrier(2), []

    def _clic():
        depart.wait()
        resultats.append(sante_agents.defaire(int(ep["id"]), ORG, acteur="jane"))
    fils = [threading.Thread(target=_clic) for _ in range(2)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    faits = [r for r in resultats if r is not None]
    assert len(faits) == 1 and resultats.count(None) == 1
    assert faits[0]["retires"] == [OUTIL]
    assert db.get_trigger(a, ORG)["tools"] == ["a"]
    gestes = episodes_agents.lire(int(ep["id"]))["reparations"]
    assert [g["action"] for g in gestes].count("annulation") == 1


def test_une_annulation_concurrente_d_une_retouche_ne_perd_pas_la_retouche(base):
    """Une retouche des outils tient la ligne de l'agent ; l'annulation lancée pendant
    elle attend, puis relit les outils APRÈS elle : la retouche n'est pas perdue, et
    l'agent ayant été écrit depuis la réparation, l'outil est gardé."""
    import threading
    from oto_mcp import db
    from oto_mcp.db import sante_agents
    from oto_mcp.db._conn import _connect
    a = _agent("Veille")
    ep = _reparer(a)
    resultat = {}

    def _annuler():
        resultat["fait"] = sante_agents.defaire(int(ep["id"]), ORG, acteur="jane")
    fil = threading.Thread(target=_annuler)
    with _connect() as conn:
        conn.execute("SELECT 1 FROM runner_triggers WHERE id = %s FOR UPDATE", (a,))
        fil.start()
        fil.join(0.5)
        assert fil.is_alive(), "l'annulation attend la ligne de l'agent"
        conn.execute("UPDATE runner_triggers SET tools = %s::jsonb, updated_at = NOW(), "
                     "updated_by = 'bob' WHERE id = %s", (f'["a", "{OUTIL}", "c"]', a))
    fil.join()
    assert resultat["fait"]["gardes"] == [OUTIL]
    assert db.get_trigger(a, ORG)["tools"] == ["a", OUTIL, "c"]


def test_l_episode_d_une_autre_org_ne_se_defait_pas(base):
    from oto_mcp.db import sante_agents
    a = _agent("Veille")
    ep = _reparer(a)
    assert sante_agents.defaire(int(ep["id"]), ORG + 1, acteur="jane") is None
    with pytest.raises(AuthzDenied) as e:
        H._defaire(ResolvedCtx(sub="jane"),
                   H.AgentHealthUndoInput(org_id=ORG + 1, episode_id=int(ep["id"])))
    assert e.value.code == "episode_not_found"


def test_qui_ne_peut_pas_modifier_l_agent_ne_le_defait_pas(base):
    from oto_mcp import db
    a = _agent("Le sien", sub="bob")
    ep = _reparer(a)
    with pytest.raises(AuthzDenied):
        _defaire(ep, sub="jane")
    assert OUTIL in db.get_trigger(a, ORG)["tools"]


def test_la_marque_d_un_outil_retire_expire(base):
    from oto_mcp.db import runner_triggers as rt
    from oto_mcp.db._conn import _connect
    a = _agent("Veille")
    _defaire(_reparer(a))
    assert OUTIL in rt.outils_annules(a, ORG)
    with _connect() as conn:
        conn.execute("UPDATE runner_triggers SET repair_undone_tools = jsonb_build_object("
                     "%s::text, '2000-01-01T00:00:00Z') WHERE id = %s", (OUTIL, a))
    assert rt.outils_annules(a, ORG) == set(), "passé le terme, le médecin peut redonner"

