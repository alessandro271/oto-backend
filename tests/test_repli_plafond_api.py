"""Le REPLI plafond → clé API, EN BASE (OTO-130, 27/09/2026).

Un travail d'abonnement dont le SEUL obstacle est un plafond de consommation à
échéance FUTURE peut rejouer sur la clé API de son org plutôt qu'attendre. Ce
que ces bancs tiennent, contre une vraie base :

1. plafond futur (mode personnel) → repli, avec le stamp `_plateforme.repli` ;
2. `needs_login` / `disconnected` → JAMAIS de repli (l'abonnement doit se
   reconnecter, ce n'est pas ce que ce chemin répare) ;
3. pool VIDE → jamais de repli (personne ne prête, ce n'est pas une pause) ;
4. pool INTÉGRALEMENT plafonné (échéance future) → repli, stamp `mode: pool` ;
5. pool MÉLANGÉ (un plafonné, un déconnecté) → jamais de repli (pas
   « intégralement » en pause) ;
6. clé API exigée et NON déposée → jamais de repli, jamais un échec dur : le
   travail reste `pending` ;
7. aucune clé exigée (défaut de la plateforme) → repli quand même, sur la clé
   de plateforme ;
8. un dépôt qui n'a pas d'équivalent d'abonnement (`mistral`) → jamais de repli.

Le mode personnel et le mode pool ont leurs bancs de RÉSERVATION ordinaire
ailleurs (`test_abonnement_personnel*.py`, `test_pool_abonnements_db.py`),
inchangés : ce fichier ne couvre que le CHEMIN NEUF.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_F = "claude_subscription"
_API = "anthropic"


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_repli_api_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "5" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _org(nom, *membres, mode=None):
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_pool as P
    admin = _personne(f"{nom}-admin")
    oid = org_store.create_org(f"Org {nom}", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, _personne(m), "org_member")
    if mode:
        P.poser_mode(oid, _F, mode, admin)
    return oid


def _abonne(sub, *, statut="connected", reset=None, pret_a=()):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected")
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _travail(org, sub, model="sub:sonnet", famille=_F):
    from oto_mcp import db
    return db.enqueue_job(org, "start", sub=sub,
                          payload={"procedure": "p", "model": model,
                                   "model_family": famille})["id"]


def _cle_org(org_id, *, connector=_API, exigee=True, deposee=True):
    from oto_mcp.db import connector_settings as CS
    from oto_mcp import credentials_store as CR
    if exigee:
        CS.set_connector_setting("org", str(org_id), connector,
                                 "runner.org_key_required", "true")
    if deposee:
        CR.set_credential("org", str(org_id), connector, "sk-test-secret")


def _repli(org, *, worker="w-api", depot=_API, org_ids=None):
    from oto_mcp import db
    return db.repli_disponible(org, org_ids, worker, depot, lease_seconds=60)


def _etat(job_id):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute("SELECT status, attempts, payload FROM runner_jobs "
                                 "WHERE id = %s", (job_id,)).fetchone())


def _futur(heures=1):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


# ── 1. plafond personnel futur → repli ───────────────────────────────────────

def test_repli_personnel_plafond_futur(live):
    oid = _org("p1", "p1-dem")
    _abonne("p1-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p1-dem")
    _cle_org(oid)

    row = _repli(oid)

    assert row is not None, "un plafond FUTUR, sans autre obstacle, se replie"
    assert row["id"] == jid
    assert row["payload"]["model"] == "claude-sonnet-5"
    assert row["payload"]["model_family"] == "anthropic"
    repli = row["payload"]["_plateforme"]["repli"]
    assert repli["from_model"] == "sub:sonnet"
    assert repli["from_family"] == "claude_subscription"
    assert repli["to_model"] == "claude-sonnet-5"
    assert repli["to_family"] == "anthropic"
    assert repli["mode"] == "personnel"
    assert repli["reason"] == "paused_limit"
    assert repli["reset_at"] is not None
    assert _etat(jid)["status"] == "claimed"


def test_repli_personnel_plafond_ECHU_ne_replie_pas(live):
    """Une échéance PASSÉE n'est plus un plafond qui bloque : la réservation
    ordinaire sert déjà ce travail (`_abonnement.servable`) — le repli n'a rien
    à faire là, `repli_disponible` ne doit donc rien trouver à replier."""
    oid = _org("p1b", "p1b-dem")
    _abonne("p1b-dem", statut="paused_limit", reset=_futur(heures=-1))
    _travail(oid, "p1b-dem")
    _cle_org(oid)

    assert _repli(oid) is None


# ── 2. needs_login / disconnected → jamais de repli ─────────────────────────

@pytest.mark.parametrize("statut", ["needs_login", "disconnected"])
def test_pas_de_repli_needs_login_ou_disconnected(live, statut):
    oid = _org(f"p2-{statut}", f"p2-{statut}-dem")
    _abonne(f"p2-{statut}-dem", statut=statut)
    jid = _travail(oid, f"p2-{statut}-dem")
    _cle_org(oid)

    assert _repli(oid) is None, (
        f"`{statut}` demande une reconnexion, pas une clé API — jamais un repli")
    assert _etat(jid)["status"] == "pending", "le travail continue d'ATTENDRE"


# ── 3. pool vide → jamais de repli ──────────────────────────────────────────

def test_pas_de_repli_pool_vide(live):
    oid = _org("p3", "p3-dem", mode="pool")
    jid = _travail(oid, "p3-dem")
    _cle_org(oid)

    assert _repli(oid) is None, "personne ne prête : ce n'est pas une pause"
    assert _etat(jid)["status"] == "pending"


# ── 4. pool intégralement plafonné (échéance future) → repli ───────────────

def test_repli_pool_integralement_plafonne(live):
    oid = _org("p4", "p4-dem", "p4-preteur", mode="pool")
    _abonne("p4-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    jid = _travail(oid, "p4-dem")
    _cle_org(oid)

    row = _repli(oid)

    assert row is not None
    assert row["id"] == jid
    assert row["payload"]["model_family"] == "anthropic"
    repli = row["payload"]["_plateforme"]["repli"]
    assert repli["mode"] == "pool"
    assert repli["reason"] == "paused_limit"


# ── 5. pool mélangé (un plafonné, un déconnecté) → jamais de repli ─────────

def test_pas_de_repli_pool_melange(live):
    oid = _org("p5", "p5-dem", "p5-a", "p5-b", mode="pool")
    _abonne("p5-a", statut="paused_limit", reset=_futur(), pret_a=[oid])
    _abonne("p5-b", statut="disconnected", pret_a=[oid])
    jid = _travail(oid, "p5-dem")
    _cle_org(oid)

    assert _repli(oid) is None, (
        "un prêteur DÉCONNECTÉ n'est pas un prêteur en pause : le pool n'est pas "
        "\"intégralement\" plafonné, et l'état du demandeur ne compte jamais ici")
    assert _etat(jid)["status"] == "pending"


# ── 6. clé exigée et non déposée → jamais de repli, jamais un échec dur ────

def test_pas_de_repli_sans_cle_deposee(live):
    oid = _org("p6", "p6-dem")
    _abonne("p6-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p6-dem")
    _cle_org(oid, exigee=True, deposee=False)

    assert _repli(oid) is None, (
        "une clé EXIGÉE et absente arrêterait le travail à `_avec_cle` — le repli "
        "doit se voir refuser la clé AVANT de prendre, pas après")
    etat = _etat(jid)
    assert etat["status"] == "pending", "jamais un échec dur : le travail attend encore"


# ── 7. aucune clé exigée (défaut plateforme) → repli quand même ────────────

def test_pas_de_repli_sans_cle_deposee_par_l_org(live):
    """Une org qui n'a RIEN déposé ne se fait pas déplacer sa dépense : le travail
    continue d'ATTENDRE la réinitialisation du forfait.

    ⚠️ C'est le cœur de la règle, et l'inverse de ce que faisait la première
    version : elle lisait `runner.org_key_required` (à `false` par défaut) et en
    concluait « aucune clé exigée, donc rien à vérifier » — or « non exigée » veut
    dire « la clé d'ENV du worker fera l'affaire », c'est-à-dire que la PLATEFORME
    aurait payé le repli de toutes les orgs sans clé. Un travail posé sur un
    abonnement est gratuit pour son org ; le déplacer vers des jetons facturés
    n'est légitime que si cette org a elle-même dit « je paie », et la seule façon
    de l'avoir dit est d'avoir déposé SA clé."""
    oid = _org("p7", "p7-dem")
    _abonne("p7-dem", statut="paused_limit", reset=_futur())
    _travail(oid, "p7-dem")
    # Aucun `_cle_org` : rien n'est réglé, rien n'est déposé.

    assert _repli(oid) is None, "sans clé de l'org, on attend — on ne dépense pas"


def test_repli_sur_la_seule_cle_deposee_sans_reglage(live):
    """Et l'inverse : la clé DÉPOSÉE suffit, sans avoir à régler quoi que ce soit.
    `runner.org_key_required` ne participe plus à cette décision — il répond à
    « qui peut tourner ? », pas à « qui PAIE ? »."""
    oid = _org("p7b", "p7b-dem")
    _abonne("p7b-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p7b-dem")
    _cle_org(oid, exigee=False, deposee=True)

    row = _repli(oid)

    assert row is not None, "une clé déposée, même sans réglage, paie le repli"
    assert row["id"] == jid


# ── 8. un dépôt sans équivalent d'abonnement → jamais de repli ─────────────

def test_pas_de_repli_pour_un_depot_sans_equivalent(live):
    oid = _org("p8", "p8-dem")
    _abonne("p8-dem", statut="paused_limit", reset=_futur())
    _travail(oid, "p8-dem")
    _cle_org(oid, connector="mistral")

    assert _repli(oid, depot="mistral") is None, (
        "aucun modèle `claude_subscription` n'a d'équivalent `mistral`")


# ── 9. le repli ne casse pas la sérialisation ni le rapport de forfait ─────

def test_apres_repli_le_travail_n_est_plus_lu_comme_un_abonnement(live):
    """Une fois reroute, `porteur_et_famille` doit voir `anthropic` — sinon le
    rapport de forfait (`_abonnement.noter_rapport`) lèverait la pause d'un
    abonnement qui n'a pourtant pas tourné (OTO-130, garde du 27/09/2026)."""
    from oto_mcp import db
    from oto_mcp.capabilities import _abonnement

    oid = _org("p9", "p9-dem")
    _abonne("p9-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p9-dem")
    _cle_org(oid)

    row = _repli(oid)
    assert row is not None

    conclu = db.porteur_et_famille(jid)
    assert conclu["model_family"] == "anthropic"
    assert not _abonnement.est_abonnement(conclu["model_family"])


# ── 10. l'interrupteur d'org : ouvert par défaut, coupable à la main ───────

def test_repli_ouvert_par_defaut_sans_reglage_d_org(live):
    """Aucune ligne de mode : le repli est OUVERT. Le défaut est sûr parce que la
    clé de l'org reste exigée par ailleurs — ouvrir ne fait dépenser personne qui
    n'a pas posé sa propre clé."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p10", "p10-dem")
    _abonne("p10-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p10-dem")
    _cle_org(oid)

    assert P.repli_api_actif(oid, "claude_subscription") is True
    row = _repli(oid)
    assert row is not None and row["id"] == jid


def test_l_org_qui_coupe_le_repli_attend_son_forfait(live):
    """L'org a une clé, et refuse quand même qu'on la dépense : ses travaux
    plafonnés ATTENDENT la réinitialisation. C'est la seule surface qui porte
    cette décision — la clé dit « je peux payer », pas « je veux payer ici »."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p11", "p11-dem")
    _abonne("p11-dem", statut="paused_limit", reset=_futur())
    _travail(oid, "p11-dem")
    _cle_org(oid)
    P.set_repli_api(oid, "claude_subscription", False, "p11-dem")

    assert P.repli_api_actif(oid, "claude_subscription") is False
    assert _repli(oid) is None, "repli coupé : on attend le forfait, on ne dépense pas"


def test_couper_le_repli_ne_change_pas_le_mode(live):
    """Couper le repli sur une org qui n'a jamais réglé de mode fait naître la
    ligne à `personnel` — son mode effectif d'avant. L'interrupteur ne doit pas
    basculer une org en pool par effet de bord."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p12", "p12-dem")
    assert P.get_mode(oid, "claude_subscription") is None

    P.set_repli_api(oid, "claude_subscription", False, "p12-dem")

    assert P.en_pool(oid, "claude_subscription") is False
    assert P.get_mode(oid, "claude_subscription")["mode"] == P.PERSONNEL


def test_le_repli_se_rouvre(live):
    """Et se rouvre : l'interrupteur n'est pas un aller simple."""
    from oto_mcp.db import org_subscription_pool as P

    oid = _org("p13", "p13-dem")
    _abonne("p13-dem", statut="paused_limit", reset=_futur())
    jid = _travail(oid, "p13-dem")
    _cle_org(oid)
    P.set_repli_api(oid, "claude_subscription", False, "p13-dem")
    assert _repli(oid) is None

    P.set_repli_api(oid, "claude_subscription", True, "p13-dem")

    row = _repli(oid)
    assert row is not None and row["id"] == jid
