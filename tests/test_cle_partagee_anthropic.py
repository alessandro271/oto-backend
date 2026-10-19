"""La clé de modèle PARTAGÉE : une clé Anthropic de plateforme (ou du tenant), servie
à une org qui n'a pas déposé la sienne et à qui elle est ACCORDÉE — puis chaque appel
au modèle réservé et réglé au budget d'unités, en jetons.

Ce que ces bancs tiennent :

1. **le connecteur déclare une clé de plateforme FERMÉE** — le grant est le droit ;
2. **à la réservation du travail, la clé de l'org passe d'abord** ; la clé partagée ne
   sert qu'une org qui n'a RIEN déposé, et le travail est estampillé avant de partir ;
3. **une clé partagée accordée vaut une clé** pour la garde d'argent : ni le
   réglage `runner.org_key_required`, ni un worker « clés d'org seules » ne
   refusent l'org à qui elle est accordée ;
4. **chaque appel au modèle est un tour du budget** : réservé avant (`op=reserve`,
   `max_tokens` de sortie), réglé après (`op=meter`, quatre comptes de jetons), les
   tours jamais rapportés réglés à la conclusion — seulement sur la tentative
   estampillée.
"""
from __future__ import annotations

import logging

import pytest

from oto_mcp import credentials_store, providers
from oto_mcp.capabilities import _cle_exigee as CE
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import ResolvedCtx

ORG = 42


# ── 1. le connecteur ───────────────────────────────────────────────────────

def test_le_connecteur_declare_une_cle_de_plateforme_FERMEE():
    c = providers.connector_for_provider("anthropic")
    assert "platform" in c.auth_modes and "byo_org" in c.auth_modes
    assert c.platform_key_open is False, "le grant de l'org EST le droit"
    assert c.kind == "credential" and c.cardinality == "mono"


# ── la résolution de la clé partagée ────────────────────────────────────────

class _RC:
    def __init__(self, mode, key="sk-partagee"):
        self.mode, self.key, self.config = mode, key, {}


def test_un_fournisseur_sans_cle_de_plateforme_n_a_pas_de_cle_partagee(monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "resolve_credential",
                        lambda *a, **k: pytest.fail("rien n'est résolu"))
    assert CE.cle_partagee(ORG, "mistral", sub="alexis") is None


@pytest.mark.parametrize("mode,attendu", [
    (credentials_store.PLATFORM, True), (credentials_store.TENANT, True),
    ("org", False), ("group", False)])
def test_seul_un_rang_partage_est_une_cle_partagee(monkeypatch, mode, attendu):
    from oto_mcp import access
    vu = {}

    def resoudre(provider, sub=None, **k):
        from oto_mcp import session_org
        vu.update(org=session_org.current_call_org(), sub=sub, k=k)
        return _RC(mode)
    monkeypatch.setattr(access, "resolve_credential", resoudre)
    rc = CE.cle_partagee(ORG, "anthropic", sub="alexis")
    assert (rc is not None) is attendu
    assert vu["org"] == ORG and vu["sub"] == "alexis", "dans l'org DU TRAVAIL"
    assert vu["k"]["check_usage"] is False and vu["k"]["emit_on_failure"] is False


def test_pas_de_grant_pas_de_cle_partagee(monkeypatch):
    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError
    from mcp.types import ErrorData, INVALID_PARAMS

    def refus(*a, **k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="no key"))
    monkeypatch.setattr(access, "resolve_credential", refus)
    assert CE.cle_partagee(ORG, "anthropic", sub="alexis") is None


def test_sans_demandeur_la_cascade_de_l_org_seule(monkeypatch):
    from oto_mcp import access
    vu = {}
    monkeypatch.setattr(access, "_resolve_credential_anon",
                        lambda p, want, org, check_usage=True:
                        vu.update(org=org, check=check_usage) or _RC("platform"))
    assert CE.cle_partagee(ORG, "anthropic") is not None
    assert vu == {"org": ORG, "check": False}


def test_une_cle_partagee_accordee_n_est_pas_manquante(monkeypatch):
    monkeypatch.setattr(CE, "cle_exigee", lambda org, f: True)
    monkeypatch.setattr(CE, "cle_deposee", lambda org, f: False)
    monkeypatch.setattr(CE, "cle_partagee", lambda org, f, sub=None: _RC("platform"))
    assert CE.manquantes(ORG, ["anthropic"]) == []
    monkeypatch.setattr(CE, "cle_partagee", lambda org, f, sub=None, strict=False: None)
    assert CE.manquantes(ORG, ["anthropic"]) == ["anthropic"]


# ── 2–3. à la réservation ───────────────────────────────────────────────────

@pytest.fixture
def remise(monkeypatch):
    """Rien de déposé par l'org, aucune exigence, une clé partagée accordée au rang
    plateforme. Chaque banc retouche ce qu'il juge."""
    vu = {"estampilles": []}
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: (None, None))
    monkeypatch.setattr(RJ, "_depot_pose", lambda org, depot: False)
    monkeypatch.setattr(CE, "cle_exigee", lambda org, f: False)
    monkeypatch.setattr(CE, "cle_partagee",
                        lambda org, f, sub=None, strict=False: vu.update(demande=(org, f, sub))
                        or _RC(credentials_store.PLATFORM))
    monkeypatch.setattr(RJ.db, "estampiller_cle_partagee",
                        lambda job_id, sub, depot, mode:
                        vu["estampilles"].append((job_id, sub, depot, mode)) or True)
    monkeypatch.setattr(RJ.db, "arreter_definitivement",
                        lambda job_id, sub, raison: vu.update(arret=raison) or True)
    monkeypatch.setattr(RJ.db, "rendre_a_la_file",
                        lambda job_id, sub, raison, delai_s=60: vu.update(rendu=raison) or True)
    monkeypatch.setattr(CE, "cle_de_plateforme_posee", lambda f: True)
    return vu


def _travail(**kw):
    return {"id": 9, "org_id": ORG, "sub": "alexis", "delegated_token": "otd_x",
            "payload": {"model_family": "anthropic"}, **kw}


def _reserver(**kw):
    kw.setdefault("metre", True)
    return RJ._avec_cle(_travail(), "anthropic", "worker:banc", worker=True, **kw)


def test_sans_cle_d_org_la_cle_partagee_part_ESTAMPILLEE(remise):
    rendu = _reserver()
    assert rendu["model_key"] == "sk-partagee"
    assert rendu["meter_usage"] is True, "le worker doit rapporter chaque appel"
    assert remise["demande"] == (ORG, "anthropic", "alexis"), "au nom du demandeur"
    assert remise["estampilles"] == [(9, "worker:banc", "anthropic", "platform")]


def test_une_estampille_qui_ne_s_ecrit_pas_ne_sert_RIEN(remise, monkeypatch):
    monkeypatch.setattr(RJ.db, "estampiller_cle_partagee", lambda *a: False)
    assert _reserver() is None, "jamais une clé qu'on ne saurait pas refacturer"


def test_la_cle_de_l_org_passe_d_abord(remise, monkeypatch):
    monkeypatch.setattr(RJ, "_cle_de_modele", lambda org, depot: ("sk-de-l-org", None))
    rendu = _reserver()
    assert rendu["model_key"] == "sk-de-l-org" and "meter_usage" not in rendu
    assert "demande" not in remise and remise["estampilles"] == []


def test_une_cle_DEPOSEE_mais_illisible_ne_bascule_pas_sur_la_partagee(remise, monkeypatch):
    """L'org paie déjà son fournisseur : la facturer sur la nôtre pour un hoquet du
    coffre la ferait payer deux fois."""
    monkeypatch.setattr(RJ, "_depot_pose", lambda org, depot: True)
    rendu = _reserver()
    assert "model_key" not in rendu and "demande" not in remise


def test_l_exigence_ne_refuse_pas_l_org_a_qui_la_cle_est_accordee(remise, monkeypatch):
    monkeypatch.setattr(CE, "cle_exigee", lambda org, f: True)
    rendu = _reserver()
    assert rendu["model_key"] == "sk-partagee" and "arret" not in remise


def test_un_worker_cles_d_org_seules_sert_la_cle_partagee(remise):
    rendu = _reserver(org_key_only=True)
    assert rendu["model_key"] == "sk-partagee" and "arret" not in remise


def test_un_worker_qui_ne_mesure_pas_ne_recoit_JAMAIS_la_cle_partagee(remise):
    """Un runner d'avant la mesure par tour tournerait sur notre clé en ne rapportant
    qu'à la fin — ou jamais. L'org y a droit : le travail retourne à la file, sans
    compter la tentative, pour un worker qui mesure."""
    assert _reserver(metre=False) is None
    assert remise["estampilles"] == [] and "rendu" in remise and "arret" not in remise


def test_la_ferme_ne_recoit_jamais_la_cle_partagee(remise):
    rendu = _reserver(ferme=True)
    assert "model_key" not in rendu and "ferme" in rendu["delegation_refusee"]
    assert remise["estampilles"] == []


def test_une_org_SANS_acces_ne_tourne_pas_sur_la_cle_du_worker(remise, monkeypatch):
    """⚠️ Le trou que la clé partagée ouvrait : l'instance paie Anthropic, l'org n'a
    ni clé ni accès (jamais accordé, ou retiré crédits épuisés) — servie sans clé,
    elle tournait sur la clé d'ENVIRONNEMENT du worker, sans rien mesurer."""
    monkeypatch.setattr(CE, "cle_partagee", lambda org, f, sub=None, strict=False: None)
    rendu = _reserver()
    assert "model_key" not in rendu
    assert "déposer" in rendu["delegation_refusee"] and "accès" in rendu["delegation_refusee"]


def test_sans_cle_de_plateforme_posee_rien_ne_change(remise, monkeypatch):
    monkeypatch.setattr(CE, "cle_partagee", lambda org, f, sub=None, strict=False: None)
    monkeypatch.setattr(CE, "cle_de_plateforme_posee", lambda f: False)
    rendu = _reserver()
    assert "delegation_refusee" not in rendu and "model_key" not in rendu


def test_sans_grant_l_exigence_arrete_toujours(remise, monkeypatch):
    monkeypatch.setattr(CE, "cle_exigee", lambda org, f: True)
    monkeypatch.setattr(CE, "cle_partagee", lambda org, f, sub=None, strict=False: None)
    rendu = _reserver()
    assert rendu["delegation_refusee"] and remise["estampilles"] == []


# ── 4. chaque appel au modèle, au budget d'unités ───────────────────────────

_TOUR = {"model": "claude-sonnet-5", "usage_input": 100, "usage_output": 50,
         "usage_cache_write": 0, "usage_cache_read": 1_000}
_ESTAMPILLE = {"depot": "anthropic", "mode": "platform", "tentative": 2}


def _prise(**kw):
    return {"org_id": ORG, "sub": "alexis", "attempts": 2, "cle_partagee": _ESTAMPILLE,
            **kw}


@pytest.fixture
def budget(monkeypatch):
    """Le budget doublé : ce que la capacité lui demande, et ce qu'il rend."""
    from oto_mcp.access import credits_tours as tours
    vu = {"reserver": [], "regler": [], "clore": [], "rendu": {"settled": True,
                                                              "amount": 7,
                                                              "exceeded": True}}
    monkeypatch.setattr(RJ.db, "tentative_en_cours", lambda job_id, sub: _prise())
    monkeypatch.setattr(tours, "reserver_tour", lambda uid, sub, org, n: vu["reserver"]
                        .append((uid, sub, org, n)) or {"held": True, "reserved": 3})
    monkeypatch.setattr(tours, "regler_tour", lambda uid, sub, org, tokens, meta:
                        vu["regler"].append((uid, sub, org, tokens, meta)) or vu["rendu"])
    monkeypatch.setattr(tours, "clore_les_tours", lambda prefixe:
                        vu["clore"].append(prefixe) or 0)
    return vu


def _ctx():
    return ResolvedCtx(sub="worker:banc", org_id=ORG)


def _reserver_tour(tour=0, max_tokens=4096):
    return RJ._jobs(_ctx(), RJ.JobsInput(op="reserve", job_id=9, turn=tour,
                                         max_tokens=max_tokens))


def _metrer(tour=0, **usage):
    return RJ._jobs(_ctx(), RJ.JobsInput(op="meter", job_id=9, turn=tour,
                                         result={**_TOUR, **usage}))


def test_un_tour_se_reserve_a_son_max_tokens_avant_l_appel(budget):
    assert _reserver_tour(3, 8000) == {"ok": True, "held": True, "reserved": 3}
    assert budget["reserver"] == [("runner:9:2:3", "alexis", ORG, 8000)], \
        "le travail, la tentative, le tour ; le demandeur et l'org du travail paient"


@pytest.mark.parametrize("code, statut", [("insufficient_credits", 402),
                                          ("credits_unpriced", 422),
                                          ("spend_ledger_unavailable", 503)])
def test_un_tour_refuse_arrete_le_worker(monkeypatch, budget, code, statut):
    from oto_mcp.access import credits_tours as tours
    from oto_mcp.capabilities._types import AuthzDenied

    def refuse(*a):
        raise tours.TourRefuse(code, "non", code == "spend_ledger_unavailable")
    monkeypatch.setattr(tours, "reserver_tour", refuse)
    with pytest.raises(AuthzDenied) as e:
        _reserver_tour()
    assert (e.value.status, e.value.code) == (statut, code)


def test_un_tour_se_regle_a_ses_quatre_comptes_de_jetons(budget):
    out = _metrer(3)
    assert out == {"ok": True, "metered": True, "amount": 7, "exceeded": True,
                   "duplicate": False}
    [(uid, sub, org, tokens, meta)] = budget["regler"]
    assert (uid, sub, org) == ("runner:9:2:3", "alexis", ORG)
    assert tokens == {"input_tokens": 100, "output_tokens": 50,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1_000}
    assert meta == {"runner_job_id": 9, "attempt": 2, "turn": 3,
                    "model": "claude-sonnet-5", "key_mode": "platform"}


@pytest.mark.parametrize("poste", ["usage_input", "usage_output"])
def test_un_tour_sans_entree_ou_sortie_est_REFUSE(budget, poste):
    """Le worker s'arrête sur ce refus : un travail ne continue pas sur notre clé
    sans que l'appel se règle."""
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _metrer(**{poste: None})
    assert e.value.code == "usage_unmeasured" and budget["regler"] == []


@pytest.mark.parametrize("valeur", [-1, 1.5, True])
def test_un_compte_de_jetons_illisible_est_REFUSE(budget, valeur):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied):
        _metrer(usage_cache_read=valeur)
    assert budget["regler"] == []


def test_un_modele_inconnu_se_regle_quand_meme_en_jetons(budget):
    """Plus de tarif : le budget compte des jetons, quel que soit le modèle."""
    assert _metrer(model="claude-inconnu")["metered"] is True


def test_un_cache_non_declare_se_lit_zero_et_le_dit(budget):
    _metrer(usage_cache_read=None)
    [(_, _, _, tokens, meta)] = budget["regler"]
    assert tokens["cache_read_input_tokens"] == 0 and meta["cache_non_declare"] is True


def test_un_rapport_rejoue_ne_se_regle_pas_deux_fois(budget):
    budget["rendu"] = {"settled": False, "amount": 0, "exceeded": False}
    assert _metrer()["duplicate"] is True


def test_un_bareme_sans_prix_ne_regle_rien_et_le_dit(budget):
    budget["rendu"] = None
    assert _metrer() == {"ok": True, "metered": False}


@pytest.mark.parametrize("mode, refuse", [("enforce", True), ("report", False)])
def test_un_registre_en_panne_au_reglement(monkeypatch, budget, mode, refuse):
    from oto_mcp.access import credits_tours as tours
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setenv("OTO_CREDITS_MODE", mode)
    monkeypatch.setattr(tours, "regler_tour", lambda *a: 1 / 0)
    if refuse:
        with pytest.raises(AuthzDenied) as e:
            _metrer()
        assert (e.value.status, e.value.code) == (503, "spend_ledger_unavailable")
    else:
        assert _metrer() == {"ok": True, "metered": False}


@pytest.mark.parametrize("prise", [_prise(cle_partagee=None),
                                   _prise(attempts=3)])   # estampille d'avant
def test_la_cle_de_l_org_n_a_rien_a_reserver_ni_a_regler(monkeypatch, budget, prise):
    monkeypatch.setattr(RJ.db, "tentative_en_cours", lambda job_id, sub: prise)
    assert _reserver_tour() == {"ok": True, "held": False}
    assert _metrer() == {"ok": True, "metered": False}
    assert budget["reserver"] == budget["regler"] == []


def test_un_travail_qui_n_est_plus_au_worker_rend_job_inconnu(monkeypatch, budget):
    from oto_mcp.capabilities._types import AuthzDenied
    monkeypatch.setattr(RJ.db, "tentative_en_cours", lambda job_id, sub: None)
    for f in (_reserver_tour, _metrer):
        with pytest.raises(AuthzDenied) as e:
            f()
        assert e.value.status == 404


def test_reserve_et_meter_sont_des_verbes_du_worker():
    assert {"reserve", "meter"} <= RJ._VERBES_DU_WORKER


@pytest.mark.parametrize("cas", ["solde_nul", "solde_negatif", "hors_bareme",
                                 "registre_qui_leve", "bareme_illisible"])
def test_en_report_aucun_tour_n_est_jamais_refuse(monkeypatch, cas):
    """`report` (le défaut, la valeur de prod au lancement) ne refuse AUCUN appel au
    modèle — ni `insufficient_credits`, ni `credits_unpriced`, ni
    `spend_ledger_unavailable` — quel que soit le solde, le barème ou l'état de la base :
    la réservation répond OK, le tour part, et son règlement aussi."""
    from oto_mcp.access import credits, credits_tours
    from oto_mcp.db import credits as db_credits
    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    monkeypatch.setattr(RJ.db, "tentative_en_cours", lambda job_id, sub: _prise())
    disponible = {"solde_nul": 0, "solde_negatif": -500}.get(cas, 10 ** 9)
    lignes = [] if cas == "hors_bareme" else [
        {"connector": "anthropic", "function": "messages", "unit": u, "budget": 1,
         "per_n": 1} for u in credits_tours.UNITES_DU_MODELE]
    monkeypatch.setattr(db_credits, "version_du_bareme",
                        (lambda: 1 / 0) if cas == "bareme_illisible" else (lambda: 1))
    monkeypatch.setattr(db_credits, "lignes_du_bareme", lambda v: lignes)
    monkeypatch.setattr(db_credits, "orgs_en_mode_personne", lambda: set())

    def reserver(**k):
        if cas == "registre_qui_leve":
            raise RuntimeError("base indisponible")
        insuffisant = disponible < k["reserved"]
        return db_credits.Reservation(posee=not insuffisant or k["forcer"], deja=False,
                                      insuffisant=insuffisant, disponible=disponible)
    monkeypatch.setattr(db_credits, "reserver", reserver)
    monkeypatch.setattr(db_credits, "reservation", lambda uid: None)
    monkeypatch.setattr(db_credits, "regler", lambda uid, **k: {"ok": 1})
    credits.oublier()
    try:
        assert _reserver_tour(0, 4096)["ok"] is True      # ne lève pas : le tour part
        out = _metrer(0)
        assert out["ok"] is True
    finally:
        credits.oublier()


def test_en_off_un_tour_n_ecrit_rien(monkeypatch):
    """`off` (le retour arrière) ne réserve ni ne règle un tour qu'il n'a pas réservé :
    rien n'est écrit au registre, le tour part et son rapport rend `metered: false`."""
    from oto_mcp.access import credits
    from oto_mcp.db import credits as db_credits
    monkeypatch.setenv("OTO_CREDITS_MODE", "off")
    monkeypatch.setattr(RJ.db, "tentative_en_cours", lambda job_id, sub: _prise())
    monkeypatch.setattr(db_credits, "reservation", lambda uid: None)
    for ecriture in ("reserver", "regler", "version_du_bareme"):
        monkeypatch.setattr(db_credits, ecriture,
                            lambda *a, _e=ecriture, **k: pytest.fail(f"{_e} en off"))
    credits.oublier()
    try:
        assert _reserver_tour(0, 4096)["ok"] is True
        assert _metrer(0)["metered"] is False
    finally:
        credits.oublier()


def _conclu(**kw):
    return {"status": "done", "run_id": "r1", "org_id": ORG, "sub": "alexis",
            "attempts": 2, "cle_partagee": _ESTAMPILLE, **kw}


def test_la_conclusion_clot_les_tours_jamais_rapportes(budget):
    RJ._clore_la_tentative(9, _conclu())
    assert budget["clore"] == ["runner:9:2:"]


def test_un_echec_qui_a_consomme_se_clot_aussi(budget):
    RJ._clore_la_tentative(9, _conclu(status="pending"))
    assert budget["clore"] == ["runner:9:2:"], "la dépense est réelle"


@pytest.mark.parametrize("conclu", [_conclu(cle_partagee=None), _conclu(attempts=3)])
def test_une_tentative_non_estampillee_ne_clot_rien(budget, conclu):
    RJ._clore_la_tentative(9, conclu)
    assert budget["clore"] == []


def test_complete_clot_ce_que_la_base_a_conclu(monkeypatch, budget):
    monkeypatch.setattr(RJ.db, "complete_job",
                        lambda job_id, sub, ok, error=None, run_id=None, result=None:
                        _conclu(run_id=None))
    RJ._jobs(_ctx(), RJ.JobsInput(op="complete", job_id=9, ok=True, result=_TOUR))
    assert budget["clore"] == ["runner:9:2:"]


# ── 6. un coffre qui ne répond pas : ni servi, ni arrêté ────────────────────

@pytest.mark.parametrize("panne", ["depot", "partagee", "posee"])
def test_un_doute_du_coffre_rend_le_travail_a_la_file(remise, monkeypatch, panne):
    """Un doute ne doit ni faire payer notre clé sans mesure, ni arrêter un travail
    avec une raison fausse (« pas d'accès, crédits ») — il retourne à la file, sans
    compter la tentative."""
    if panne == "depot":
        monkeypatch.setattr(RJ, "_depot_pose", lambda org, depot: None)
    elif panne == "partagee":
        def illisible(org, f, sub=None, strict=False):
            raise CE.CleIllisible("coffre")
        monkeypatch.setattr(CE, "cle_partagee", illisible)
    else:
        monkeypatch.setattr(CE, "cle_partagee", lambda org, f, sub=None, strict=False: None)
        monkeypatch.setattr(CE, "cle_de_plateforme_posee", lambda f: None)
    assert _reserver() is None
    assert "coffre illisible" in remise["rendu"]
    assert "arret" not in remise and remise["estampilles"] == []


def test_la_reservation_demande_le_doute_au_lieu_de_le_taire(remise, monkeypatch):
    vu = {}
    monkeypatch.setattr(CE, "cle_partagee",
                        lambda org, f, sub=None, strict=False: vu.update(strict=strict)
                        or _RC("platform"))
    _reserver()
    assert vu["strict"] is True


def test_cle_partagee_stricte_leve_sur_un_coffre_qui_casse(monkeypatch):
    from oto_mcp import access

    def casse(*a, **k):
        raise RuntimeError("pool")
    monkeypatch.setattr(access, "resolve_credential", casse)
    assert CE.cle_partagee(ORG, "anthropic", sub="alexis") is None
    with pytest.raises(CE.CleIllisible):
        CE.cle_partagee(ORG, "anthropic", sub="alexis", strict=True)
