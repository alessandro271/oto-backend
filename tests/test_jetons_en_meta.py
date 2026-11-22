"""`oto-mcp maintenance jetons-en-meta` : retirer de `connector_credentials.meta` les clés
de forme jeton, pour la liste fermée des connecteurs.

À blanc par défaut ; `--appliquer` retire la clé (`meta - 'clé'`) et rien d'autre : la
ligne reste, son secret, ses autres satellites et `set_at` aussi. Un connecteur hors de la
liste est refusé avant toute lecture ; un satellite scellé (`session_jar_enc` du
connecteur `api`) n'est jamais touché. Idempotent, relu après coup, jamais une valeur
imprimée.
"""
from __future__ import annotations

import json

import pytest

from oto_mcp import credentials_store, maintenance
from oto_mcp.connectors import jetons_en_meta as jm

VALEUR = "VALEUR-SECRETE-A-NE-JAMAIS-IMPRIMER"


def _exec(sql, params=()):
    from oto_mcp import db
    with db._connect() as conn:
        return conn.execute(sql, params)


def _poser(connector: str, entity_id: str, meta: dict, account: str = "") -> None:
    _exec("INSERT INTO connector_credentials (entity_type, entity_id, connector, account, "
          "secret_enc, meta) VALUES ('org', %s, %s, %s, 'chiffre', %s)",
          (entity_id, connector, account, json.dumps(meta)))


def _ligne(connector: str, entity_id: str, account: str = "") -> dict:
    return dict(_exec("SELECT secret_enc, meta, set_at FROM connector_credentials "
                      "WHERE entity_type = 'org' AND entity_id = %s AND connector = %s "
                      "AND account = %s", (entity_id, connector, account)).fetchone())


@pytest.fixture(autouse=True)
def table_rase(live):
    _exec("DELETE FROM connector_instances")
    _exec("DELETE FROM connector_credentials")


def _seme() -> None:
    _poser("google", "1", {"access_token": VALEUR, "expires_at": "2026-01-01T00:00:00Z",
                           "scopes": "s", "is_default": True}, account="a@x.test")
    _poser("google", "2", {"access_token": VALEUR, "scopes": "s"}, account="b@x.test")
    _poser("google", "3", {"scopes": "s"}, account="c@x.test")      # déjà propre
    _poser("memento", "1", {"access_token": VALEUR, "id_token": VALEUR, "base": "b"})
    _poser("atlassian", "1", {"cloud_id": "c"})
    # Hors de la liste : jamais touchés, même avec une clé de forme jeton.
    _poser("api", "1", {"session_jar_enc": "scelle", "access_token": VALEUR})
    _poser("hubspot", "1", {"access_token": VALEUR})


# ── avant toute lecture ──────────────────────────────────────────────────────

def test_la_liste_des_cles_est_celle_des_surfaces_de_statut():
    assert jm.cles() == credentials_store.BEARER_META_KEYS
    assert set(jm.cles()) <= set(credentials_store._SECRET_META_KEYS)
    assert not any(c.endswith("_enc") for c in jm.cles()), "un scellé n'est pas un jeton"
    assert jm.CONNECTEURS == ("google", "memento", "atlassian", "folkmcp")


@pytest.mark.parametrize("noms", [["api"], ["google", "hubspot"], []])
def test_un_connecteur_hors_liste_est_refuse_avant_toute_lecture(monkeypatch, noms):
    from oto_mcp import db
    monkeypatch.setattr(db, "_connect", lambda *a, **k: pytest.fail("rien ne doit être lu"))
    with pytest.raises(jm.ConnecteurHorsListe):
        jm.nettoyer(appliquer=True, connecteurs=noms)


def test_la_commande_rend_le_code_du_refus(monkeypatch, capsys):
    from oto_mcp import db
    monkeypatch.setattr(db, "_connect", lambda *a, **k: pytest.fail("rien ne doit être lu"))
    assert maintenance.main(["jetons-en-meta", "--appliquer", "--connecteur", "api"]) == \
        maintenance.JETONS_REFUS
    assert "REFUS" in capsys.readouterr().err


# ── sur la base ──────────────────────────────────────────────────────────────

def test_a_blanc_rien_n_est_ecrit_et_aucune_valeur_n_est_imprimee(live, capsys):
    _seme()
    rapport = jm.nettoyer(appliquer=False)
    assert rapport["a_retirer"] == {"google": {"access_token": 2},
                                    "memento": {"access_token": 1, "id_token": 1}}
    assert _ligne("google", "1", "a@x.test")["meta"]["access_token"] == VALEUR
    assert maintenance.main(["jetons-en-meta"]) == maintenance.JETONS_FAIT
    sortie = capsys.readouterr()
    assert "À BLANC" in sortie.out and "access_token 2" in sortie.out
    assert VALEUR not in sortie.out + sortie.err


def test_appliquer_retire_la_cle_seulement(live, capsys, caplog):
    _seme()
    avant = _ligne("google", "1", "a@x.test")
    caplog.set_level("INFO")
    assert maintenance.main(["jetons-en-meta", "--appliquer"]) == maintenance.JETONS_FAIT
    sortie = capsys.readouterr()
    assert "APPLIQUÉ" in sortie.out
    assert VALEUR not in sortie.out + sortie.err + caplog.text
    apres = _ligne("google", "1", "a@x.test")
    assert apres["meta"] == {"expires_at": "2026-01-01T00:00:00Z", "scopes": "s",
                             "is_default": True}
    assert apres["secret_enc"] == avant["secret_enc"] and apres["set_at"] == avant["set_at"]
    assert _ligne("memento", "1")["meta"] == {"base": "b"}
    # Aucune ligne supprimée ; hors liste, rien ne bouge.
    assert _exec("SELECT count(*) AS n FROM connector_credentials").fetchone()["n"] == 7
    assert _ligne("api", "1")["meta"] == {"session_jar_enc": "scelle", "access_token": VALEUR}
    assert _ligne("hubspot", "1")["meta"] == {"access_token": VALEUR}


def test_restreint_a_un_connecteur_de_la_liste(live):
    _seme()
    rapport = jm.nettoyer(appliquer=True, connecteurs=["memento"])
    assert rapport["retire"] == {"memento": {"access_token": 1, "id_token": 1}}
    assert rapport["reste"] == {}
    assert _ligne("google", "1", "a@x.test")["meta"]["access_token"] == VALEUR


def test_idempotent(live):
    _seme()
    premier = jm.nettoyer(appliquer=True)
    assert premier["retire"] == {"google": {"access_token": 2},
                                 "memento": {"access_token": 1, "id_token": 1}}
    assert not jm.reste(premier)
    second = jm.nettoyer(appliquer=True)
    assert second["retire"] == {} and second["reste"] == {}
