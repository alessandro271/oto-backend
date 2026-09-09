"""La file d'exécutions du runner — les gardes que le worker ne doit jamais contourner.

Ce que ces tests verrouillent : le scope org du claim (un worker ne voit que SA
file), les exigences par kind (un `continue` sans run est inexécutable, autant le
refuser à l'entrée), le refus-sans-oracle sur les verbes de prise (conclure le job
d'un autre = job inconnu), et la borne du bail. Le comportement SQL (backoff,
`failed` au plafond, SKIP LOCKED) est porté par `db/runner_jobs.py` et se vérifie
au déploiement — ici on stubbe, on teste la capacité.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _ctx(sub="worker-campagne", org_id=226):
    return ResolvedCtx(sub=sub, org_id=org_id)


def _appel(ctx, **kw):
    return RJ._jobs(ctx, RJ.JobsInput(**kw))


@pytest.fixture
def espion(monkeypatch):
    vu = {}
    # ⚠️ La doublure suit la SIGNATURE SERVIE : `fleet_id` est entré avec le
    # rattachement d'un travail à sa flotte (#791). Une doublure figée sur une
    # ancienne signature ne protège plus rien — elle éclate en `TypeError`, ce qui
    # est le bon comportement : c'est le contrat qui a bougé, pas le test.
    monkeypatch.setattr(RJ.db, "enqueue_job",
                        # ⚠️ `**_` : une doublure qui fige la signature de son
                        # original casse au premier champ ajouté — et l'échec
                        # accuse le test, pas le manque.
                        lambda org_id, kind, payload=None, run_id=None,
                        max_attempts=3, fleet_id=None, sub=None, **_:
                        vu.update(org=org_id, kind=kind, fleet=fleet_id,
                                  sub=sub) or
                        {"id": 7, "status": "pending", "due_at": "2026-08-13",
                         "fleet_id": fleet_id})
    monkeypatch.setattr(RJ.db, "claim_next_job",
                        lambda org_id, sub, lease_seconds=600:
                        vu.update(claim=(org_id, sub, lease_seconds)) or None)
    monkeypatch.setattr(RJ.db, "complete_job",
                        lambda job_id, sub, ok, error=None, run_id=None, result=None:
                        vu.update(result=result) or
                        ({"status": "done"} if sub == "worker-campagne" else None))
    monkeypatch.setattr(RJ.db, "bind_job_run", lambda j, s, r: s == "worker-campagne")
    monkeypatch.setattr(RJ.db, "extend_job_lease", lambda j, s, lease_seconds=600: False)
    monkeypatch.setattr(RJ.db, "get_job", lambda j, org: None)
    # ⚠️ Doublure OBLIGATOIRE : sans elle, l'arrêt des campagnes épuisées tape la
    # vraie base, lève, et le fail-open de la production avale l'échec — les
    # bancs de campagne passeraient alors sans rien exercer. Vérifié le
    # 07/09/2026 : lancés seuls ils tombaient, en groupe ils passaient.
    monkeypatch.setattr(RJ.db, "arreter_campagnes_epuisees", lambda org_id: [])
    return vu


# ── le scope, sans lequel tout le reste est faux ──────────────────────────────

def test_le_claim_porte_lorg_et_le_sub_de_lappelant(espion):
    _appel(_ctx(), op="claim")
    org, sub, _ = espion["claim"]
    assert (org, sub) == (226, "worker-campagne"), \
        "le claim ne peut servir QUE la file de l'org du jeton, au nom du worker"


def test_un_worker_SANS_org_reserve_quand_meme(espion):
    """Renversement du 08/09/2026. Ce banc exigeait `org_required` sur `claim`,
    et c'est ce refus qui imposait un worker par organisation.

    Un worker n'a pas d'organisation : il exécute, il ne décide de rien, et son
    droit d'agir vient du jeton délégué émis au nom du demandeur. `org_id=None`
    n'est pas une garde retirée, c'est l'absence d'une notion qui n'a pas de
    sens pour une machine."""
    _appel(ResolvedCtx(sub="w", org_id=None), op="claim")
    assert espion["claim"][0] is None, (
        "l'org du worker ne filtre plus la file : il prend le travail le plus "
        "ancien, tous clients confondus")


def test_les_verbes_D_HUMAIN_exigent_toujours_une_org(espion):
    """L'autre bord, et sans lui on aurait ouvert bien plus que la file :
    enfiler un travail, lire la file ou ouvrir un travail restent des gestes
    d'organisation. Les libérer laisserait voir la file d'autrui."""
    for op in ("enqueue", "list", "get"):
        with pytest.raises(AuthzDenied) as e:
            _appel(ResolvedCtx(sub="w", org_id=None), op=op, job_id=1, kind="start",
                   payload={"procedure": "p"})
        assert e.value.code == "org_required", op


def test_le_bail_est_borne(espion):
    _appel(_ctx(), op="claim", lease_seconds=999_999)
    assert espion["claim"][2] == 3600, "un bail d'une journée n'est pas un heartbeat"
    _appel(_ctx(), op="claim", lease_seconds=1)
    assert espion["claim"][2] == 30, "un bail d'une seconde est un claim jetable"


# ── les exigences par kind, refusées à l'entrée ───────────────────────────────

def test_un_continue_sans_run_est_refuse(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="enqueue", kind="continue")
    assert e.value.code == "missing_fields" and "run_id" in str(e.value.message)


def test_un_start_sans_payload_est_refuse(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="enqueue", kind="start")
    assert e.value.code == "missing_fields"


def test_enfiler_un_continue_sur_le_run_dautrui_rend_run_inconnu(espion, monkeypatch):
    """Le gate propriétaire tient CÔTÉ SERVEUR, pas dans le séquencement de l'UI :
    un enqueue direct (sans append préalable) sur le run d'un autre ferait
    continuer son fil par le worker, avec les droits du run. Même 404 sans
    oracle que l'append du fil (R1)."""
    monkeypatch.setattr(RJ.db, "get_run_head",
                        lambda run_id: {"sub": "proprietaire", "org_id": 226}
                        if run_id == "run-X" else None)
    monkeypatch.setattr(RJ.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne s'enfile sans propriété"))
    with pytest.raises(AuthzDenied) as a:
        _appel(_ctx(sub="intrus"), op="enqueue", kind="continue", run_id="run-X")
    with pytest.raises(AuthzDenied) as b:
        _appel(_ctx(sub="intrus"), op="enqueue", kind="continue", run_id="run-INEXISTANT")
    assert (a.value.status, a.value.code) == (b.value.status, b.value.code) == \
        (404, "run_not_found")


def test_le_proprietaire_enfile_son_continue(espion, monkeypatch):
    monkeypatch.setattr(RJ.db, "get_run_head",
                        lambda run_id: {"sub": "worker-campagne", "org_id": 226})
    out = _appel(_ctx(), op="enqueue", kind="continue", run_id="run-X")
    assert out["id"] == 7


def test_enqueue_scope_lorg_de_lappel(espion):
    out = _appel(_ctx(org_id=42), op="enqueue", kind="start",
                 payload={"procedure": "veille-linkedin"})
    assert espion["org"] == 42 and out["id"] == 7


# ── conclure ce qui ne nous appartient pas = job inconnu (pas d'oracle) ───────

def test_conclure_le_job_dun_autre_rend_job_inconnu(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(sub="autre-worker"), op="complete", job_id=7, ok=True)
    assert (e.value.status, e.value.code) == (404, "job_not_found")


def test_le_claimant_conclut(espion):
    out = _appel(_ctx(), op="complete", job_id=7, ok=True)
    # Sans run connu, la libération des baux n'est pas tentée et la réponse le DIT
    # (#633) : null + raison, jamais un 0 fabriqué.
    assert out == {"ok": True, "status": "done",
                   "run_id": None, "rows_released": None, "release": "no_run"}


def test_prolonger_un_bail_perdu_rend_job_inconnu(espion):
    # Le bail a expiré, un autre worker a re-claimé : extend rend rowcount 0.
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="extend", job_id=7)
    assert e.value.status == 404, \
        "un worker dont le bail est mort ne garde aucune prise sur le job"


# ── le résultat déclaré (R5, garde budget de flotte) ─────────────────────────

def test_complete_transporte_le_resultat_declare(espion):
    _appel(_ctx(), op="complete", job_id=7, ok=True,
           result={"usage_tokens": 31500, "stopped": "end_turn", "steps": 18})
    assert espion["result"] == {"usage_tokens": 31500, "stopped": "end_turn",
                                "steps": 18}, \
        "le coût d'un job doit être LISIBLE par l'ordonnanceur de flotte"


def test_un_resultat_obese_est_refuse(espion):
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="complete", job_id=7, ok=True,
               result={"note": "x" * 5000})
    assert e.value.code == "result_too_large", \
        "result est un résumé, jamais un contenu de fil"


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os
    import uuid as _uuid

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_rjobs_" + _uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def test_la_liste_est_scopee_a_lorg_et_filtrable(live):
    """La surveillance (page Automatisations) : la file de MON org seulement,
    du plus récent au plus ancien, filtrable par statut."""
    from oto_mcp import db as d

    a = d.enqueue_job(310, "start", payload={"procedure": "p1"})
    d.enqueue_job(311, "start", payload={"procedure": "autrui"})
    job = d.claim_next_job(310, "w-list", lease_seconds=60)
    d.complete_job(job["id"], "w-list", False, error="boom")   # pending, attempt 1

    jobs = d.list_jobs(310)
    assert all("autrui" not in str(j.get("payload")) for j in jobs), \
        "la file d'une autre org ne doit JAMAIS apparaître"
    assert any(j["id"] == a["id"] for j in jobs)
    en_attente = d.list_jobs(310, status="pending")
    assert {j["status"] for j in en_attente} == {"pending"}


def test_le_resultat_fait_l_aller_retour_en_base(live):
    """Le round-trip RÉEL : complete écrit `result`, get le rend — c'est ce que
    l'ordonnanceur de flotte lira pour sa garde budget. Un stub ne prouve ni la
    colonne, ni le COALESCE, ni le SELECT."""
    from oto_mcp import db as d

    j = d.enqueue_job(226, "start", payload={"procedure": "p"})
    job = d.claim_next_job(226, "worker-live", lease_seconds=60)
    assert job and job["id"] == j["id"]
    out = d.complete_job(job["id"], "worker-live", True,
                         result={"usage_tokens": 12345, "stopped": "end_turn"})
    assert out == {"status": "done", "run_id": None}, \
        "complete rend le run connu du job (#633) — aucun ici"
    relu = d.get_job(job["id"], 226)
    assert relu["result"] == {"usage_tokens": 12345, "stopped": "end_turn"}
    assert relu["status"] == "done"


# ── La campagne produit son travail au SONDAGE, sans ordonnanceur ────────────
# Un worker ne connaît pas la notion de campagne : il demande du travail. Quand
# la file est vide, c'est ICI qu'on décide s'il y en a un à fabriquer. Ce qui
# remplace un ordonnanceur externe qu'un humain lançait à la main, qui prenait
# la campagne, la découpait, et battait pour dire qu'il vivait.

CAMPAGNE = {"id": 12, "org_id": 7, "sub": "celui-qui-a-declare", "label": "passage-editeurs",
            "procedure": "enrichissement", "project_id": 220, "namespace": "tableau",
            "tools": ["data_claim_next", "data_write"], "input": "file {namespace}",
            "row_filter": {"statut": "a_enrichir"}, "max_steps": 40,
            "max_tokens_per_row": 80000}


@pytest.fixture
def campagne(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id: espion.update(cherchee=org_id) or CAMPAGNE)
    monkeypatch.setattr(RJ.db, "marquer_demarree",
                        lambda fid: espion.update(demarree=fid))
    return espion


def test_une_file_vide_fait_produire_le_travail_de_la_campagne(campagne):
    _appel(_ctx(), op="claim")
    assert campagne["cherchee"] == 226, "la campagne se cherche dans l'org du worker"
    assert campagne["fleet"] == 12, "le travail doit être rattaché à sa campagne"
    assert campagne["kind"] == "start"


def test_le_travail_porte_l_identite_du_DECLARANT_pas_du_worker(campagne):
    """C'est la garde qui compte. Le worker n'est pas un pouvoir : il portera un
    jeton émis au nom de quelqu'un d'autre. Prendre son propre `sub` ici ferait
    agir la campagne sous l'identité de l'infrastructure."""
    _appel(_ctx(sub="worker-campagne"), op="claim")
    assert campagne["sub"] == "celui-qui-a-declare"
    assert campagne["sub"] != "worker-campagne"


def test_la_campagne_passe_a_running_au_premier_travail(campagne):
    _appel(_ctx(), op="claim")
    assert campagne["demarree"] == 12


def test_sans_campagne_le_sondage_rend_simplement_rien(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id: None)
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    assert _appel(_ctx(), op="claim") == {"job": None}
    assert "fleet" not in espion, "aucun travail ne doit être fabriqué"


def test_une_campagne_illisible_ne_casse_PAS_le_sondage(monkeypatch, espion):
    """Fail-open : le sondage des workers est le chemin le plus fréquent de toute
    la plateforme. Une campagne mal formée ne doit pas l'arrêter pour l'org —
    le passage attendra le sondage suivant."""
    def _explose(org_id):
        raise RuntimeError("colonne manquante")
    monkeypatch.setattr(RJ.db, "campagne_a_servir", _explose)

    # Ne CASSE pas — mais ne se tait pas non plus. Ce test affirmait
    # `== {"job": None}`, c'est-à-dire exactement le silence qui a laissé le
    # sondage répondre « rien à faire » pendant des jours alors qu'il n'avait
    # jamais réussi à regarder (07/09/2026). Le worker reçoit la cause.
    rendu = _appel(_ctx(), op="claim")

    assert rendu["job"] is None, "une campagne illisible ne casse pas le sondage"
    assert "RuntimeError" in rendu["campaign_error"], (
        "et elle se DIT au worker : « rien à faire » et « je n'ai pas pu "
        "regarder » ne sont pas la même réponse")


def test_file_vide_ne_porte_aucune_panne(monkeypatch, espion):
    """Le pendant, sans lequel le champ ne prouve rien : une file réellement
    vide ne doit porter AUCUN signalement. Un champ toujours présent redevient
    du bruit, et on aurait juste déplacé le silence."""
    monkeypatch.setattr(RJ.db, "campagne_a_servir", lambda org_id: None)
    assert _appel(_ctx(), op="claim") == {"job": None}


def test_une_campagne_cassee_ne_journalise_QU_UNE_fois(monkeypatch, espion, caplog):
    """Le sondage tourne en boucle sur chaque worker. Journaliser à chaque tour
    noierait le journal sous des milliers de lignes identiques — et un journal
    noyé ne se lit pas, ce qui revient à ne rien dire. On veut le contraire :
    une ligne qui se voit."""
    RJ._CAMPAGNE_MUETTE.clear()
    def _explose(org_id):
        raise RuntimeError("colonne manquante")
    monkeypatch.setattr(RJ.db, "campagne_a_servir", _explose)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            _appel(_ctx(), op="claim")
    lignes = [r for r in caplog.records if "production de travail impossible" in r.message]
    assert len(lignes) == 1, f"5 sondages ont produit {len(lignes)} lignes de journal"


def test_une_cause_DIFFERENTE_se_dit(monkeypatch, espion, caplog):
    """Ne pas répéter n'est pas se taire : une panne qui change de nature est une
    information neuve, et l'étouffer ferait manquer la seconde."""
    RJ._CAMPAGNE_MUETTE.clear()
    causes = iter(["colonne manquante", "colonne manquante", "table absente"])
    def _explose(org_id):
        raise RuntimeError(next(causes))
    monkeypatch.setattr(RJ.db, "campagne_a_servir", _explose)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            _appel(_ctx(), op="claim")
    lignes = [r for r in caplog.records if "production de travail impossible" in r.message]
    assert len(lignes) == 2, "deux causes distinctes, deux lignes"


def test_les_campagnes_epuisees_sont_arretees_AVANT_d_en_servir_une(monkeypatch, espion, caplog):
    """L'ordonnanceur qui portait cette borne s'arrêtait quand personne ne le
    lançait. Le sondage, lui, ne s'arrête jamais : une campagne qui échoue en
    boucle régénérerait du travail toute la nuit. Et il faut l'ARRÊTER, pas
    seulement la sauter — sinon elle reste `running` sans avancer."""
    ordre = []
    monkeypatch.setattr(RJ.db, "arreter_campagnes_epuisees",
                        lambda org_id: ordre.append("arret") or [77])
    monkeypatch.setattr(RJ.db, "campagne_a_servir",
                        lambda org_id: ordre.append("service") or None)
    monkeypatch.setattr(RJ.db, "marquer_demarree", lambda fid: None)
    with caplog.at_level("WARNING"):
        _appel(_ctx(), op="claim")
    assert ordre == ["arret", "service"], "arrêter d'abord, servir ensuite"
    assert any("campagne 77 arrêtée" in r.message for r in caplog.records), \
        "un arrêt automatique qui ne se dit pas est un silence de plus"



# ── La consigne servie DANS le cadre, pas chargée par l'agent ────────────────
# Mesuré le 08/09/2026 sur une passe réelle : la consigne que l'agent charge au
# premier tour est FACTURÉE plein tarif au deuxième — 20 603 jetons sur 41 204,
# la moitié du déroulé, avec un cache à zéro sur ce tour-là. Jointe au travail à
# la réservation, elle entre dans le préfixe stable : lue en cache dès le
# premier tour, et le tour de chargement disparaît.
#
# ⚠️ Jointe, JAMAIS stockée : une consigne pèse ~20 000 caractères, et cent
# travaux la porteraient cent fois en base. Même raison que la clé de modèle.

def test_le_travail_reserve_porte_le_TEXTE_de_sa_procedure(monkeypatch, espion):
    monkeypatch.setattr(RJ.db, "get_guide_db",
                        lambda scope, owner, slug: {"body_md": "LA CONSIGNE"})
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "passe-registre"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert job["system"] == "LA CONSIGNE", (
        "le worker reçoit du TEXTE à poser en cadre — il ne va rien chercher, "
        "et il ignore ce qu'est une procédure")


def test_une_procedure_ABSENTE_ne_fait_pas_echouer_la_reservation(monkeypatch, espion):
    """L'agent la chargera lui-même, comme avant. C'est une accélération,
    jamais une condition — un travail ne se perd pas parce qu'un texte manque."""
    monkeypatch.setattr(RJ.db, "get_guide_db", lambda scope, owner, slug: None)
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "jamais-posee"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert "system" not in job
    assert job["id"] == 7, "le travail est servi quand même"


def test_une_lecture_de_procedure_qui_LEVE_ne_perd_pas_le_travail(monkeypatch, espion):
    """Le pendant du précédent, et le plus important des trois : la base peut
    tousser. Un travail réservé qui se perdrait ici laisserait sa ligne sous
    bail jusqu'à expiration."""
    def _explose(scope, owner, slug):
        raise RuntimeError("base indisponible")
    monkeypatch.setattr(RJ.db, "get_guide_db", _explose)
    monkeypatch.setattr(RJ.db, "claim_next_job", lambda *a, **k: {
        "id": 7, "org_id": 226, "sub": "demandeur",
        "payload": {"procedure": "passe-registre"}})

    job = _appel(_ctx(), op="claim")["job"]

    assert "system" not in job and job["id"] == 7
