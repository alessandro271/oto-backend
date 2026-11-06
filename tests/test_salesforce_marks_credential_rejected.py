"""oto#25 lot b2 — Salesforce marque la ligne de coffre rejetée sur un refus du
REFRESH (`SalesforceAuthError`, levée par `oto.tools.salesforce.client` — la seule
exception qui vaut « grant mort »), et RE-LÈVE toujours l'exception d'origine.

Voisin de `test_salesforce_op_dispatch.py` (le routage `op=`) mais un sujet
différent : ici, le marquage. Le contrefactuel (`test_une_erreur_applicative_
ordinaire_ne_marque_rien`) est la garde nommée dans l'issue : un 401 nu d'un geste
précis (permission manquante sur UN enregistrement, clé par ailleurs saine) ne doit
jamais être confondu avec un grant mort.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from oto.tools.salesforce import SalesforceAuthError
from oto_mcp.tools import salesforce as S


def _tool(name: str):
    from fastmcp import FastMCP

    m = FastMCP("t")
    S.register(m)
    return asyncio.run(m.get_tool(name)).fn


_HOTE = "x.my.salesforce.com"
_PUBLIQUE = {"93.184.216.34"}


class _RC:
    """Substitut de `ResolvedCredential` — une entité au palier MEMBRE (flaggable)."""
    entity_type, entity_id, account = "member", "2:sub-x", ""
    fields = {"client_id": "ci", "client_secret": "cs", "refresh_token": "rt",
              "login_url": f"https://{_HOTE}"}


@pytest.fixture
def env(monkeypatch, dns_simule):
    """Client Salesforce factice qui lève `SalesforceAuthError` sur chaque méthode —
    même patron de fixture que `test_salesforce_op_dispatch.py`, avec un espion sur
    `credentials_store.update_meta` en plus."""
    import oto.tools.salesforce.client as sf_client
    from oto_mcp import access, credentials_store

    inst = MagicMock()
    boom = SalesforceAuthError("invalid_grant: token revoked")
    inst.list_records.side_effect = boom
    inst.query.side_effect = boom
    inst.list_notes.side_effect = boom
    inst.describe.side_effect = boom
    monkeypatch.setattr(sf_client, "SalesforceClient", lambda **kw: inst)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RC())
    dns_simule[_HOTE] = _PUBLIQUE   # le garde de sortie résout `login_url`

    calls = []

    def _update_meta(entity_type, entity_id, connector, account, patch, conn=None):
        calls.append((entity_type, entity_id, connector, account, patch))
        return True

    monkeypatch.setattr(credentials_store, "update_meta", _update_meta)
    return inst, calls


@pytest.mark.parametrize("tool,kwargs", [
    ("salesforce_record", {"sobject": "Contact"}),
    ("salesforce_query", {"query": "SELECT Id FROM Account"}),
    ("salesforce_note", {"record_id": "003a"}),
    ("salesforce_describe", {"sobject": "Account"}),
])
def test_un_grant_mort_marque_la_ligne_et_relance(env, tool, kwargs):
    _inst, calls = env
    with pytest.raises(SalesforceAuthError):
        _tool(tool)(**kwargs)
    assert len(calls) == 1, "le rejet doit être marqué UNE fois, sur la ligne réellement servie"
    entity_type, entity_id, connector, account, patch = calls[0]
    assert (entity_type, entity_id, connector, account) == (
        "member", "2:sub-x", "salesforce", "")
    assert patch["health_ko"] is True
    assert "invalid_grant" in patch["health_reason"]


def test_une_erreur_applicative_ordinaire_ne_marque_rien(monkeypatch, dns_simule):
    """Contrefactuel : une erreur applicative (permission manquante sur UN
    enregistrement, avec une clé par ailleurs saine) n'est PAS un
    `SalesforceAuthError` — c'est le type de l'exception, jamais un statut deviné,
    qui décide du marquage."""
    import oto.tools.salesforce.client as sf_client
    from oto_mcp import access, credentials_store

    inst = MagicMock()
    inst.list_records.side_effect = ValueError("INSUFFICIENT_ACCESS on Contact")
    monkeypatch.setattr(sf_client, "SalesforceClient", lambda **kw: inst)
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RC())
    dns_simule[_HOTE] = _PUBLIQUE   # le garde de sortie résout `login_url`

    calls = []
    monkeypatch.setattr(credentials_store, "update_meta",
                        lambda *a, **k: calls.append(a) or True)

    # `EgressRefused` est aussi une `ValueError` : on vise l'erreur du client, pas un
    # refus du garde qui ferait passer l'épreuve à vide.
    with pytest.raises(ValueError, match="INSUFFICIENT_ACCESS"):
        _tool("salesforce_record")(sobject="Contact")
    inst.list_records.assert_called_once()
    assert calls == [], "une erreur applicative ordinaire ne doit jamais marquer la clé rejetée"
