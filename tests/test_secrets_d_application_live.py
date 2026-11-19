"""Les secrets d'application OAuth de l'instance, rangés au coffre — sur une VRAIE base.

Ce que ce banc prouve, sur PostgreSQL et sous une clé maître :
- `credentials_store.set_app_secret` scelle (la colonne ne porte jamais le clair), se
  relit, se liste sans valeur, se retire ; jamais servi par la cascade (aucun de ces
  connecteurs ne déclare le mode plateforme, `tests/test_editor_app.py`) ;
- le lecteur `auth/app_instance` prend le public dans `connector_settings`, le secret au
  coffre, et JAMAIS un secret resté en clair dans la table ;
- la console admin (`platform.connector.setting`) route une clé de forme secret au
  coffre, refuse une portée d'org, et ne rend que « posé » ;
- `oto-mcp maintenance secrets-d-application` : à blanc ne touche à rien ;
  `--sans-effacer` scelle et garde le clair ; `--appliquer` scelle, vérifie, efface ;
  rejouée, elle ne fait rien ; un écart n'est pas tranché ; une ancienne carte suit son
  porteur ; aucune valeur n'est imprimée.

`pg_dsn` (conftest) : `OTO_TEST_PG_DSN`, sinon un conteneur jetable, sinon skip.
"""
from __future__ import annotations

import base64
import os
import types
import uuid

import pytest

_KEY = base64.b64encode(b"\x37" * 32).decode()
# Des valeurs reconnaissables, pour vérifier qu'aucune ne sort.
SECRETS = {("microsoft", "client_secret"): "valeur-ms-A1",
           ("jefacture", "client_secret"): "valeur-jf-B2",
           ("instagram_meta", "app_secret"): "valeur-ig-C3",
           ("sharepoint", "client_secret"): "valeur-ms-A1"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_appsec_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = _KEY
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url), ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _rows(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


@pytest.fixture(autouse=True)
def table_rase(live):
    _exec("DELETE FROM connector_instances")
    _exec("DELETE FROM connector_credentials")
    _exec("DELETE FROM connector_settings")


def _en_clair(connector, key, value):
    """Une ligne d'AVANT la garde (l'écrivain la refuse désormais)."""
    _exec("INSERT INTO connector_settings (scope_type, scope_id, connector, key, value) "
          "VALUES ('platform', 'platform', %s, %s, %s)", (connector, key, value))


def _clairs():
    return {(r["connector"], r["key"]) for r in _rows(
        "SELECT connector, key FROM connector_settings")}


def _prod():
    """Les quatre lignes en clair d'une base d'avant, et les ids publics."""
    for (c, k), v in SECRETS.items():
        _en_clair(c, k, v)
    for c, k in (("microsoft", "client_id"), ("jefacture", "client_id"),
                 ("instagram_meta", "app_id"), ("sharepoint", "client_id")):
        _en_clair(c, k, f"id-{c}")


# ── le coffre ────────────────────────────────────────────────────────────────

def test_le_secret_est_scelle_relu_liste_retire(live):
    from oto_mcp import credentials_store as cs
    cs.set_app_secret("slack", "signing_secret", "valeur-sl-D4", set_by="admin")
    brut = _rows("SELECT secret_enc FROM connector_credentials WHERE connector='slack'")
    assert brut and "valeur-sl-D4" not in str(brut[0]["secret_enc"])
    assert cs.get_app_secrets("slack", ["signing_secret", "client_secret"]) == {
        "signing_secret": "valeur-sl-D4"}
    poses = cs.app_secrets_set("slack")
    assert [(r["key"], r["set_by"]) for r in poses] == [("signing_secret", "admin")]
    assert "valeur" not in str(poses)
    # Hors de l'app d'éditeur et des apps dédiées.
    assert cs.list_editor_apps("slack") == []
    assert cs.clear_app_secret("slack", "signing_secret") is True
    assert cs.get_app_secrets("slack", ["signing_secret"]) == {}


def test_le_lecteur_ne_prend_jamais_un_secret_en_clair(live):
    from oto_mcp import credentials_store as cs
    from oto_mcp.auth import jefacture
    _en_clair("jefacture", "client_id", "id-jf")
    _en_clair("jefacture", "client_secret", "valeur-jf-B2")
    # Le clair est là, le coffre est vide : le secret est MANQUANT, refus nommé.
    assert jefacture.coordonnees_manquantes() == ["client_secret"]
    with pytest.raises(RuntimeError, match="client_secret"):
        jefacture.app()
    cs.set_app_secret("jefacture", "client_secret", "valeur-jf-B2")
    assert jefacture.app() == ("id-jf", "valeur-jf-B2")


def test_les_lecteurs_de_chaque_connecteur_lisent_le_coffre(live):
    from oto_mcp import credentials_store as cs
    from oto_mcp.auth import hubspot, linkedin_ads, meta_ads, microsoft, slack
    from oto_mcp.db import connector_settings as store
    for c, k in (("microsoft", "client_id"), ("linkedin_ads", "client_id"),
                 ("slack", "client_id"), ("hubspot", "client_id"),
                 ("hubspot", "user_client_id"), ("meta_ads", "app_id"),
                 ("meta_ads", "config_id")):
        store.set_connector_setting("platform", "platform", c, k, f"id-{c}-{k}")
    for c, k in (("microsoft", "client_secret"), ("linkedin_ads", "client_secret"),
                 ("slack", "client_secret"), ("slack", "signing_secret"),
                 ("hubspot", "client_secret"), ("hubspot", "user_client_secret"),
                 ("meta_ads", "app_secret")):
        cs.set_app_secret(c, k, f"s-{c}-{k}")
    assert microsoft.app()["client_secret"] == "s-microsoft-client_secret"
    assert linkedin_ads.app()[1] == "s-linkedin_ads-client_secret"
    assert slack.app()["client_secret"] == "s-slack-client_secret"
    assert slack.signing_secret() == "s-slack-signing_secret"
    assert hubspot.app(hubspot.APP_USER)[1] == "s-hubspot-user_client_secret"
    assert hubspot.app_ready_scopes("sub") == ["member", "org", "group"]
    assert meta_ads.coordonnees_manquantes() == []


# ── la console admin ─────────────────────────────────────────────────────────

def _console(op, **kw):
    from oto_mcp.capabilities import platform_connectors as pc
    return pc._connector_setting(types.SimpleNamespace(sub="admin"),
                                 pc.ConnectorSettingInput(op=op, **kw))


def test_la_console_range_le_secret_au_coffre(live):
    from oto_mcp import credentials_store as cs
    from oto_mcp.capabilities._types import AuthzDenied
    _console("set", connector="instagram_meta", key="app_secret", value="valeur-ig-C3")
    _console("set", connector="instagram_meta", key="app_id", value="id-ig")
    assert _clairs() == {("instagram_meta", "app_id")}
    assert cs.get_app_secrets("instagram_meta", ["app_secret"]) == {
        "app_secret": "valeur-ig-C3"}
    vue = _console("list", key="app_secret")
    assert "valeur-ig-C3" not in str(vue)
    ligne = next(r for r in vue["rows"] if r["key"] == "app_secret")
    assert "not shown" in ligne["value"] and ligne["set_by"] == "admin"
    with pytest.raises(AuthzDenied) as e:
        _console("set", connector="instagram_meta", key="app_secret", value="x", org_id=4)
    assert e.value.code == "invalid_setting"
    assert _console("clear", connector="instagram_meta", key="app_secret")["changed"]
    assert cs.get_app_secrets("instagram_meta", ["app_secret"]) == {}


# ── la migration ─────────────────────────────────────────────────────────────

def _lancer(capsys, *args):
    from oto_mcp import maintenance
    code = maintenance.main(["secrets-d-application", *args])
    sortie = capsys.readouterr()
    texte = sortie.out + sortie.err
    for valeur in SECRETS.values():
        assert valeur not in texte, "une valeur est imprimée"
    return code, texte


def test_a_blanc_rien_n_est_ecrit(live, capsys):
    from oto_mcp import maintenance
    _prod()
    code, texte = _lancer(capsys)
    assert code == maintenance.SECRETS_RESTE
    assert "microsoft.client_secret : à déplacer" in texte
    assert "sharepoint.client_secret : copie périmée" in texte
    assert len(_clairs()) == 8
    assert _rows("SELECT 1 FROM connector_credentials") == []


def test_les_deux_phases_sans_fenetre(live, capsys):
    """Phase 1 (`--sans-effacer`) : le coffre porte le secret ET le clair reste — le code
    d'avant comme celui d'après lisent. Phase 3 (`--appliquer`) : le clair disparaît."""
    from oto_mcp import credentials_store as cs, maintenance
    from oto_mcp.auth import instagram_meta, jefacture, microsoft
    _prod()
    code, texte = _lancer(capsys, "--appliquer", "--sans-effacer")
    assert code == maintenance.SECRETS_FAIT, texte
    assert "jefacture.client_secret : copié au coffre, clair conservé" in texte
    assert len(_clairs()) == 8
    assert microsoft.app() == {"client_id": "id-microsoft", "client_secret": "valeur-ms-A1"}
    assert jefacture.app() == ("id-jefacture", "valeur-jf-B2")
    # Rejouée : rien de neuf.
    code, texte = _lancer(capsys, "--appliquer", "--sans-effacer")
    assert code == maintenance.SECRETS_FAIT and "déjà au coffre" in texte

    code, texte = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_FAIT, texte
    assert "microsoft.client_secret : déjà au coffre, clair effacé" in texte
    assert "sharepoint.client_secret : copie périmée d'une ancienne carte, effacée" in texte
    assert _clairs() == {("microsoft", "client_id"), ("jefacture", "client_id"),
                         ("instagram_meta", "app_id"), ("sharepoint", "client_id")}
    assert instagram_meta.coordonnees_manquantes() == []
    assert microsoft.app()["client_secret"] == "valeur-ms-A1"
    # Le coffre ne garde rien sous l'ancienne carte.
    assert cs.get_app_secrets("sharepoint", ["client_secret"]) == {}
    code, texte = _lancer(capsys)
    assert code == maintenance.SECRETS_FAIT and "aucun secret" in texte


def test_en_une_fois(live, capsys):
    from oto_mcp import maintenance
    from oto_mcp.auth import jefacture
    _prod()
    code, texte = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_FAIT, texte
    assert "jefacture.client_secret : déplacé" in texte
    assert not {k for _, k in _clairs()} & {"client_secret", "app_secret"}
    assert jefacture.app()[1] == "valeur-jf-B2"


def test_une_ancienne_carte_seule_va_sous_son_porteur(live, capsys):
    from oto_mcp import credentials_store as cs, maintenance
    _en_clair("sharepoint", "client_secret", "valeur-ms-A1")
    code, texte = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_FAIT, texte
    assert cs.get_app_secrets("microsoft", ["client_secret"]) == {
        "client_secret": "valeur-ms-A1"}
    assert _clairs() == set()


def test_un_ecart_n_est_pas_tranche(live, capsys):
    from oto_mcp import credentials_store as cs, maintenance
    _en_clair("jefacture", "client_secret", "valeur-jf-B2")
    cs.set_app_secret("jefacture", "client_secret", "autre-valeur-E5")
    code, texte = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_REFUS
    assert "jefacture.client_secret : écart" in texte and "autre-valeur-E5" not in texte
    assert ("jefacture", "client_secret") in _clairs()
    assert cs.get_app_secrets("jefacture", ["client_secret"])["client_secret"] == \
        "autre-valeur-E5"


def test_un_secret_hors_plateforme_n_est_pas_touche(live, capsys):
    from oto_mcp import maintenance
    _exec("INSERT INTO connector_settings (scope_type, scope_id, connector, key, value) "
          "VALUES ('org', '4', 'slack', 'client_secret', 'valeur-jf-B2')")
    code, texte = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_REFUS
    assert "slack.client_secret [org] : hors palier plateforme" in texte
    assert _clairs() == {("slack", "client_secret")}


def test_une_relecture_en_echec_laisse_la_ligne_intacte(live, capsys, monkeypatch):
    from oto_mcp import credentials_store as cs, maintenance
    _en_clair("jefacture", "client_secret", "valeur-jf-B2")
    vrai = cs.get_app_secrets
    monkeypatch.setattr(cs, "get_app_secrets",
                        lambda c, k, conn=None: {kk: "corrompu" for kk in vrai(c, k, conn=conn)})
    code, _ = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_ERREUR
    monkeypatch.setattr(cs, "get_app_secrets", vrai)
    assert ("jefacture", "client_secret") in _clairs()
    assert vrai("jefacture", ["client_secret"]) == {}, "le scellé doit être annulé"


def test_sans_cle_maitre_refus(live, capsys, monkeypatch):
    from oto_mcp import maintenance
    monkeypatch.delenv("OTO_MCP_MASTER_KEY")
    code, texte = _lancer(capsys, "--appliquer")
    assert code == maintenance.SECRETS_ERREUR and "OTO_MCP_MASTER_KEY" in texte


def test_sans_effacer_exige_appliquer(live):
    from oto_mcp import maintenance
    with pytest.raises(SystemExit):
        maintenance.main(["secrets-d-application", "--sans-effacer"])
