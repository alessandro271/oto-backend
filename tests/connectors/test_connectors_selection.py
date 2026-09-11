"""Sélection marketplace (`connectors.*`) — projection compacte (#109) + guidage
d'activation (#111). Seams de domaine monkeypatchés (pas de DB)."""
import pytest

from oto_mcp.capabilities.connectors import selection as CS
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx


def _catalog(monkeypatch, entries):
    monkeypatch.setattr(CS, "_visible_catalog", lambda ctx: list(entries))
    monkeypatch.setattr(CS.connector_selection, "list_selection_detail", lambda sub, org: {})
    monkeypatch.setattr(CS.connector_selection, "list_removed", lambda sub, org: {})
    monkeypatch.setattr(CS.org_store, "get_org_default_connectors", lambda org: [])
    monkeypatch.setattr(CS, "_guide_refs_by_ns", lambda org: {})


# ── #109 : projection compacte par défaut, plein sur verbose ──

_FAT = {"name": "serper", "label": "Serper", "help": "recherche web", "family": "api",
        "category": "Prospection", "availability": "self_serve", "logo_url": None,
        "secret_kind": "api_key",
        "namespaces": ["serper"], "doc_sections": [{"body_md": "x" * 5000}],
        "credential_fields": [{"name": "api_key"}], "auth": {"method": "secret"}}


def test_me_compact_by_default_drops_heavy_fields(monkeypatch):
    _catalog(monkeypatch, [_FAT])
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput())
    c = out["connectors"][0]
    assert out["verbose"] is False
    assert c["name"] == "serper" and c["state"] == "not_selected"
    # Les gros champs ne sont PAS dans la vue compacte.
    for heavy in ("doc_sections", "credential_fields", "auth"):
        assert heavy not in c


def test_me_compact_keeps_secret_kind(monkeypatch):
    """`secret_kind` reste en compact alors que le reste de l'auth part.

    C'est la seule chose du mode compact qui distingue « il faut apporter une
    clé » de « ça marche sans rien » (les onze connecteurs en `none`). Sans lui
    la carte du front ne pouvait rien dire d'un connecteur avant qu'on l'ouvre,
    et `auth`, qui porte la même réponse, coûte la projection verbeuse entière.
    """
    _catalog(monkeypatch, [_FAT, {**_FAT, "name": "osm", "secret_kind": "none"}])
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput())
    kinds = {c["name"]: c["secret_kind"] for c in out["connectors"]}
    assert kinds == {"serper": "api_key", "osm": "none"}
    # …et il ne traîne PAS `auth` avec lui : c'est un scalaire, pas une porte
    # ouverte sur la carte verbeuse.
    assert "auth" not in out["connectors"][0]


def test_me_verbose_keeps_full_card(monkeypatch):
    _catalog(monkeypatch, [_FAT])
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(verbose=True))
    c = out["connectors"][0]
    assert out["verbose"] is True and c["doc_sections"] and c["auth"]["method"] == "secret"


def test_me_state_filter(monkeypatch):
    _catalog(monkeypatch, [_FAT, {**_FAT, "name": "hunter"}])
    monkeypatch.setattr(CS.connector_selection, "list_selection_detail",
                        lambda sub, org: {"hunter": {"state": "active", "origin": "membre"}})
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(state="active"))
    assert [c["name"] for c in out["connectors"]] == ["hunter"]


# ── oto#42 / oto-backend#868 : un unselect qui ne retire rien REFUSE ──────────

def test_unselect_reussi_rend_removed_true(monkeypatch):
    monkeypatch.setattr(CS.connector_selection, "unselect", lambda sub, name, org: True)
    out = CS._unselect(ResolvedCtx(sub="u1", org_id=42), CS.ConnectorActionInput(name="hunter"))
    assert out == {"connector": "hunter", "state": "not_selected", "removed": True}


def test_unselect_sans_ligne_a_retirer_refuse_au_lieu_de_repondre_ok(monkeypatch):
    """Le défaut signalé (oto-backend#868) : `unselect` sur un connecteur déjà
    non-sélectionné (ou sélectionné sous une autre org active) ne trouve aucune
    ligne — `rowcount=0` — et ça ne doit plus jamais se lire comme un succès."""
    monkeypatch.setattr(CS.connector_selection, "unselect", lambda sub, name, org: False)
    with pytest.raises(AuthzDenied) as e:
        CS._unselect(ResolvedCtx(sub="u1", org_id=42), CS.ConnectorActionInput(name="instagram"))
    assert e.value.code == "connector_not_selected" and e.value.status == 404
    assert "instagram" in e.value.message


def test_unselect_scope_lappel_sur_org_id_ou_zero(monkeypatch):
    """`ctx.org_id or 0` : la même règle que `_select`/`_pause`, un espace perso
    (org_id=None côté ctx) écrit/lit sous la sentinelle 0, jamais None en SQL."""
    seen = []
    monkeypatch.setattr(CS.connector_selection, "unselect",
                        lambda sub, name, org: seen.append((sub, name, org)) or True)
    CS._unselect(ResolvedCtx(sub="u1", org_id=None), CS.ConnectorActionInput(name="hunter"))
    assert seen == [("u1", "hunter", 0)]


# ── #326 : filtre `name` (lecture d'état ciblée, plus d'échec silencieux) ──

def test_me_name_filter_returns_only_that_connector(monkeypatch):
    _catalog(monkeypatch, [_FAT, {**_FAT, "name": "hunter"}])
    out = CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(name="hunter"))
    assert [c["name"] for c in out["connectors"]] == ["hunter"]


def test_me_unknown_name_raises_instead_of_returning_everything(monkeypatch):
    """Le mode d'échec de #326 : `name` ignoré → catalogue entier (~30k tokens en
    verbose) sans warning. Une liste vide serait tout aussi muette → on lève."""
    import pytest
    from oto_mcp.capabilities._types import AuthzDenied
    _catalog(monkeypatch, [_FAT])
    with pytest.raises(AuthzDenied) as e:
        CS._me(ResolvedCtx(sub="u1", org_id=42), CS.MyConnectorsInput(name="zohodesk"))
    assert e.value.code == "unknown_connector"


# ── #111 : guidage d'activation (oto_call comme pont) ──

def test_select_returns_activation_hint(monkeypatch):
    monkeypatch.setattr(CS.connector_activation, "exposed_connectors", lambda org: {"unipile"})
    calls = []
    monkeypatch.setattr(CS.connector_selection, "set_state",
                        lambda sub, name, state, org: calls.append((sub, name, state, org)))
    # #186 : la réponse DONNE les noms d'outils (registre boot, immunisé visibilité).
    import oto_mcp.tool_registry as tr
    monkeypatch.setattr(tr, "boot_tool_names",
                        lambda: ["unipile_me", "unipile_search", "zoho_record"])
    out = CS._select(ResolvedCtx(sub="u1", org_id=42), CS.ConnectorActionInput(name="unipile"))
    assert out["connector"] == "unipile" and out["state"] == "active"
    assert out["tools"] == ["unipile_me", "unipile_search"]   # les siens, pas zoho
    assert "oto_call" in out["hint"] and "unipile_me" in out["hint"]
    assert calls and calls[0][1] == "unipile"


# Les gestes du KIT (bulk_select, unset_default, recommend) sont éprouvés sur un
# vrai PostgreSQL par `tests/connectors/test_kit_application.py` (oto#166).
