"""Les secrets d'application OAuth de l'instance, simulés pour les tests sans base.

Les tests d'un connecteur simulent ses coordonnées par des lignes de
`connector_settings` (`list_connector_settings` remplacé). Depuis que les secrets sont
au coffre (`auth/app_instance`), le lecteur ne prend plus un secret dans ces lignes :
`coffre_depuis_les_reglages` fait servir par le coffre simulé les clés de forme secret
des lignes simulées — relues à chaque appel, donc un test qui remplace ses lignes
change aussi son coffre.
"""
from __future__ import annotations


def coffre_depuis_les_reglages(monkeypatch) -> None:
    from oto_mcp import credentials_store
    from oto_mcp.db import connector_settings as store

    def _secrets(connector) -> dict:
        return {r["key"]: r["value"] for r in store.list_connector_settings()
                if r["scope_type"] == "platform" and r["connector"] == connector
                and r["value"] and store.a_la_forme_d_un_secret(connector, r["key"])}

    monkeypatch.setattr(
        credentials_store, "get_app_secrets",
        lambda connector, keys, conn=None: {k: v for k, v in _secrets(connector).items()
                                            if k in keys})
    monkeypatch.setattr(
        credentials_store, "app_secrets_set",
        lambda connector=None: [
            {"connector": c, "key": k, "set_by": None, "set_at": None}
            for c in ({connector} if connector else
                      {r["connector"] for r in store.list_connector_settings()})
            for k in _secrets(c)])
    # La présence se lit dans l'inventaire du palier plateforme (`auth/app_instance`).
    monkeypatch.setattr(
        credentials_store, "list_all_platform_instances",
        lambda: {c: [{"label": credentials_store.INSTANCE_APP_LABEL, "account": k}
                     for k in _secrets(c)]
                 for c in {r["connector"] for r in store.list_connector_settings()}})
