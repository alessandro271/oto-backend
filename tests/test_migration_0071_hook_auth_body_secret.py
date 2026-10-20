"""La révision `0071_hook_auth_body_secret` réécrit le préréglage webhook `lemlist` (20/10/2026).

Le code ne connaît plus `lemlist` (ni préréglage, ni alias) : la garde SQL des livraisons
filtre par NOM, une ligne restée `lemlist` serait fermée. La révision la pose sous
`body_secret`, sans toucher au porteur ; son retour arrière est l'inverse exact. Joué sur
une vraie base : ce qu'on vérifie, c'est ce que le stockage porte après la montée.
"""
from __future__ import annotations

from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
_PRECEDENTE = "0070_triggers_outils_plateforme"
_REVISION = "0071_hook_auth_body_secret"


def _alembic():
    from alembic.config import Config
    cfg = Config(str(RACINE / "alembic.ini"))
    cfg.set_main_option("script_location", str(RACINE / "oto_mcp" / "db" / "migrations"))
    return cfg


def _lire(dsn, sql, *args):
    import psycopg
    with psycopg.connect(dsn) as c:
        return c.execute(sql, args).fetchall()


@pytest.fixture(scope="module")
def base_montee(live, pg_module_dsn):
    """Une base bootée, estampillée à la révision d'avant, avec un agent par
    préréglage (dont un `lemlist`) — puis montée par la révision."""
    import psycopg
    from alembic import command

    command.stamp(_alembic(), _PRECEDENTE)
    with psycopg.connect(pg_module_dsn, autocommit=True) as c:
        for tid, mode in ((91001, "lemlist"), (91002, "bearer"),
                          (91003, "standard_webhooks"), (91004, "none")):
            c.execute("INSERT INTO runner_triggers (id, org_id, sub, procedure, tools, "
                      "kind, hook_auth, hook_secret_hash) VALUES (%s, 1, 'u1', 'p', '[]', "
                      "'webhook', %s, %s)", (tid, mode, f"h{tid}"))
    command.upgrade(_alembic(), _REVISION)
    return pg_module_dsn


def test_une_ligne_lemlist_passe_en_body_secret_porteur_intact(base_montee):
    assert _lire(base_montee, "SELECT hook_auth, hook_secret_hash FROM runner_triggers "
                              "WHERE id = 91001") == [("body_secret", "h91001")]


def test_les_autres_prereglages_ne_bougent_pas(base_montee):
    assert _lire(base_montee, "SELECT id, hook_auth FROM runner_triggers "
                              "WHERE id IN (91002, 91003, 91004) ORDER BY id") == [
        (91002, "bearer"), (91003, "standard_webhooks"), (91004, "none")]


def test_plus_aucun_lemlist_et_le_retour_arriere_est_l_inverse_exact(base_montee):
    from alembic import command

    assert _lire(base_montee, "SELECT id FROM runner_triggers "
                              "WHERE hook_auth = 'lemlist'") == []
    command.downgrade(_alembic(), _PRECEDENTE)
    try:
        assert _lire(base_montee, "SELECT hook_auth FROM runner_triggers "
                                  "WHERE id = 91001") == [("lemlist",)]
    finally:
        command.upgrade(_alembic(), _REVISION)
