"""Le TEXTE d'une péremption de cycle nomme la VRAIE cause (27/09/2026, trigger
OTO-130) — pas une garantie de comportement (la RÈGLE d'expiration ne
bouge pas), un diagnostic. Avant ce lot, `perimer_travaux_du_declencheur`
disait toujours « aucun agent ne dessert cette organisation », même quand un
worker de la famille existait et que seule la connexion personnelle du
propriétaire (ou un pool en pause) manquait — deux diagnostics qui n'envoient
pas au même geste : l'un dit « préviens l'exploitant », l'autre dit
« reconnecte-toi, ça repart tout seul ».

Ce fichier tient `_abonnement.raison_de_peremption` contre une vraie base pour
les lectures d'abonnement/pool, et vérifie que le texte calculé est bien celui
qui atterrit dans `last_error` (`perimer_travaux_du_declencheur`)."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_F = "claude_subscription"


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_raison_perime_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "6" * 64
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


def _futur(heures=1):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


def _servie(*familles):
    return {"armed": True, "families": list(familles)}


def _raison(*a, **kw):
    from oto_mcp.capabilities import _abonnement
    return _abonnement.raison_de_peremption(*a, **kw)


# ── aucun modèle déclaré : la seule question qui vaille est « un runner existe-t-il ? »

def test_sans_modele_et_sans_runner(live):
    r = _raison(None, 1, None, {"armed": False, "families": []})
    assert "Aucun agent ne dessert cette organisation." in r


def test_sans_modele_mais_un_runner_existe(live):
    r = _raison(None, 1, None, {"armed": True, "families": ["mistral"]})
    assert "Aucun agent ne dessert cette organisation." not in r


# ── famille API déclarée : servie ou pas ────────────────────────────────────

def test_famille_api_non_servie(live):
    r = _raison("qui@x", 1, "anthropic", _servie("mistral"))
    assert "Aucun agent ne dessert cette organisation." in r


def test_famille_api_servie(live):
    r = _raison("qui@x", 1, "anthropic", _servie("anthropic"))
    assert "Aucun agent ne dessert cette organisation." not in r


# ── abonnement personnel : la vraie cause, pas le texte générique ──────────

def test_abonnement_famille_non_servie_du_tout(live):
    oid = _org("r1", "r1-dem")
    _abonne("r1-dem", statut="needs_login")
    r = _raison("r1-dem", oid, _F, _servie())
    assert "Aucun agent ne dessert cette organisation." in r


def test_abonnement_doit_se_reconnecter(live):
    oid = _org("r2", "r2-dem")
    _abonne("r2-dem", statut="needs_login")
    r = _raison("r2-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "doit se reconnecter" in r


def test_abonnement_deconnecte(live):
    oid = _org("r3", "r3-dem")
    _abonne("r3-dem", statut="disconnected")
    r = _raison("r3-dem", oid, _F, _servie(_F))
    assert "doit se reconnecter" in r


def test_abonnement_plafond_futur(live):
    oid = _org("r4", "r4-dem")
    _abonne("r4-dem", statut="paused_limit", reset=_futur())
    r = _raison("r4-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "plafond de consommation" in r


def test_abonnement_servable_maintenant_garde_le_texte_generique(live):
    """Plafond ÉCHU (ou jamais connu) : la réservation ordinaire sert déjà ce
    travail — la vraie cause a cédé, le texte générique reste le plus honnête."""
    oid = _org("r5", "r5-dem")
    _abonne("r5-dem", statut="connected")
    r = _raison("r5-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." in r


# ── pool ─────────────────────────────────────────────────────────────────────

def test_pool_vide(live):
    oid = _org("r6", "r6-dem", mode="pool")
    r = _raison("r6-dem", oid, _F, _servie(_F))
    assert "pool" in r and "vide" in r


def test_pool_en_pause(live):
    oid = _org("r7", "r7-dem", "r7-preteur", mode="pool")
    _abonne("r7-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    r = _raison("r7-dem", oid, _F, _servie(_F))
    assert "pool" in r and "pause" in r


# ── la BASE (attendu ou pas) porte toujours l'ouverture, sans « aucun agent » ni
#    « aucun modèle » : c'est ce que le texte GARDE de la version d'avant ────

def test_le_socle_est_toujours_present(live):
    for r in (
        _raison(None, 1, None, {"armed": True, "families": []}),
        _raison("x", 1, "anthropic", _servie("anthropic")),
    ):
        assert r.startswith(
            "occurrence non prise dans son cycle : le déclencheur a enfilé la "
            "suivante.")


# ── intégration : le texte calculé atterrit bien dans `last_error` ─────────

def test_le_texte_calcule_atterrit_dans_last_error(live):
    from oto_mcp import db

    oid = _org("r8", "r8-dem")
    _abonne("r8-dem", statut="paused_limit", reset=_futur())
    job = db.enqueue_job(oid, "start", sub="r8-dem",
                         payload={"procedure": "p", "model": "sub:sonnet",
                                  "model_family": _F, "trigger_id": 999})

    raison = _raison("r8-dem", oid, _F, _servie(_F))
    n = db.perimer_travaux_du_declencheur(999, oid, raison=raison)
    assert n == 1

    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute("SELECT status, last_error FROM runner_jobs WHERE id = %s",
                           (job["id"],)).fetchone()
    assert row["status"] == "expired"
    assert row["last_error"] == raison
    assert "plafond de consommation" in row["last_error"]
