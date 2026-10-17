"""Les accords et les refus nominatifs d'une clé PLATEFORME suivent la personne quand ses
comptes fusionnent (#1258) — exercé sur le merge réel (`db.migrate_sub`), en SQL réel.

Trois endroits d'une ligne de coffre nomment `user:<sub>` hors du `share_side` (#1251) :
`meta.share_denied` (la révocation F-046), `share_down` (les bénéficiaires d'une clé
fermée) et `meta.rate_limit_by` (`{scope: quota journalier}`). La fusion les laissait sur
l'ancien sub : une révocation se perdait (le compte fusionné était servi à nouveau), un
accord ou un quota nominatif ne suivait pas. Pour chaque colonne : la fusion A → A', et
le canonique déjà présent (pas de doublon ; deux quotas → le plus restrictif).

`live` (conftest) : base neuve du module, bootée par le vrai chemin.
"""
from __future__ import annotations

import json
import uuid

import pytest

_FOURNISSEUR = "aiark"   # connecteur à clé plateforme payante, hors chaîne d'arêtes


@pytest.fixture
def cle(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "7" * 64)


@pytest.fixture
def sans_cle_plateforme(live):
    """Chaque test pose SA clé : une révocation vaut pour toutes les clés du connecteur,
    une clé d'un test voisin fausserait la résolution."""
    from oto_mcp import credentials_store
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM connector_credentials WHERE entity_type = %s "
                     "AND connector = %s", (credentials_store.PLATFORM, _FOURNISSEUR))


def _personnes(*prefixes):
    from oto_mcp import db
    u = uuid.uuid4().hex[:8]
    subs = [f"{p}_{u}" for p in prefixes]
    for sub in subs:
        db.upsert_user(sub, email=f"{sub}@exemple.test")
    return subs


def _ligne(label, connector=_FOURNISSEUR):
    from oto_mcp import credentials_store
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT share_mode, share_down, meta, version FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s "
            "AND account = ''", (credentials_store.PLATFORM, label, connector)).fetchone()


def _ligne_brute(label, connector, down, meta, mode="closed"):
    """Une ligne plateforme posée telle quelle : on teste ce que le STOCKAGE porte après
    la fusion, le secret n'est jamais lu."""
    from oto_mcp import credentials_store
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "INSERT INTO connector_credentials (entity_type, entity_id, connector, "
            "account, secret_enc, share_mode, share_down, meta) VALUES "
            "(%s, %s, %s, '', 'chiffre-factice', %s, %s::jsonb, %s::jsonb)",
            (credentials_store.PLATFORM, label, connector, mode, json.dumps(down),
             json.dumps(meta)))


def _cle_ouverte(label="commune"):
    """Une clé payante ouverte à tous (`open`, `share_down` vide)."""
    from oto_mcp import credentials_store, grants_chain
    assert not grants_chain.is_chained(_FOURNISSEUR)
    credentials_store.set_credential(credentials_store.PLATFORM, label, _FOURNISSEUR,
                                     "k-1", set_by="op")
    assert _ligne(label)["share_mode"] == "open"


def _cle_fermee(label):
    """Une clé payante FERMÉE (`closed`, `share_down` vide = personne)."""
    from oto_mcp import credentials_store
    from oto_mcp.db._conn import _connect
    _cle_ouverte(label)
    with _connect() as conn:
        conn.execute("UPDATE connector_credentials SET share_mode = 'closed' "
                     "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
                     (credentials_store.PLATFORM, label, _FOURNISSEUR))


# ── share_denied : la révocation suit (point de sécurité) ────────────────────

def test_la_revocation_suit_le_compte_fusionne(live, cle, sans_cle_plateforme,
                                              monkeypatch):
    """Bout en bout : A révoqué sur une clé ouverte à tous ; après A → A', A' est REFUSÉ
    par la résolution réelle, et un tiers reste servi."""
    from oto_mcp import access, credentials_store, db
    from oto_mcp.access import resolve
    from oto_mcp.mcp_errors import McpError
    ancien, canonique, tiers = _personnes("ancien", "canonique", "tiers")
    _cle_ouverte()
    credentials_store.platform_revoke(_FOURNISSEUR, f"user:{ancien}")
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    with pytest.raises(McpError):
        resolve._resolve_credential_impl(_FOURNISSEUR, "auto", ancien)
    # Contrefactuel : avant la fusion, le canonique est servi (la clé est ouverte).
    assert resolve._resolve_credential_impl(_FOURNISSEUR, "auto", canonique).is_platform

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    with pytest.raises(McpError):
        resolve._resolve_credential_impl(_FOURNISSEUR, "auto", canonique)
    servi = resolve._resolve_credential_impl(_FOURNISSEUR, "auto", tiers)
    assert servi.is_platform and servi.secret == "k-1"
    assert _ligne("commune")["meta"]["share_denied"] == [f"user:{canonique}"]
    # La ligne reste déchiffrable : `meta` est hors AAD.
    assert credentials_store.get_credential(credentials_store.PLATFORM, "commune",
                                            _FOURNISSEUR) == "k-1"


def test_revocation_canonique_deja_present_sans_doublon(live):
    from oto_mcp import db
    ancien, canonique = _personnes("ancien", "canonique")
    _ligne_brute("refus", "hunter", [], {"share_denied": [
        f"user:{ancien}", "org:42", f"user:{canonique}", "group:3"]}, mode="open")

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    assert _ligne("refus", "hunter")["meta"] == {
        "share_denied": [f"user:{canonique}", "org:42", "group:3"]}


# ── share_down : l'accord nominatif suit ─────────────────────────────────────

def test_l_accord_nominatif_suit_le_compte_fusionne(live, cle, sans_cle_plateforme,
                                                  monkeypatch):
    from oto_mcp import access, credentials_store, db
    from oto_mcp.access import resolve
    from oto_mcp.mcp_errors import McpError
    ancien, canonique = _personnes("ancien", "canonique")
    _cle_fermee("fermee")
    credentials_store.platform_grant(_FOURNISSEUR, f"user:{ancien}")
    assert _ligne("fermee")["share_down"] == [f"user:{ancien}"]
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    with pytest.raises(McpError):           # contrefactuel : pas encore à lui
        resolve._resolve_credential_impl(_FOURNISSEUR, "auto", canonique)

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    assert resolve._resolve_credential_impl(_FOURNISSEUR, "auto", canonique).is_platform
    ligne = _ligne("fermee")
    assert ligne["share_down"] == [f"user:{canonique}"]
    assert ligne["share_mode"] == "closed"


def test_accord_canonique_deja_present_sans_doublon(live):
    from oto_mcp import db
    ancien, canonique, tiers = _personnes("ancien", "canonique", "tiers")
    _ligne_brute("liste", "hunter",
                 [f"user:{ancien}", "org:41", f"user:{canonique}", f"user:{tiers}"], {})
    _ligne_brute("autre", "hunter", ["org:41", f"user:{tiers}"], {"rate_limit": 5})
    version_autre = _ligne("autre", "hunter")["version"]

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    assert _ligne("liste", "hunter")["share_down"] == [
        f"user:{canonique}", "org:41", f"user:{tiers}"]
    # Une ligne qui ne nomme pas l'ancien n'est même pas réécrite.
    intacte = _ligne("autre", "hunter")
    assert intacte["share_down"] == ["org:41", f"user:{tiers}"]
    assert intacte["meta"] == {"rate_limit": 5}
    assert intacte["version"] == version_autre


# ── rate_limit_by : le quota suit ; deux quotas → le plus restrictif ─────────

def test_le_quota_nominatif_suit_le_compte_fusionne(live, cle, sans_cle_plateforme,
                                                   monkeypatch):
    from oto_mcp import access, credentials_store, db
    from oto_mcp.access.platform_grant import _platform_grant_meta
    ancien, canonique = _personnes("ancien", "canonique")
    _cle_ouverte("quota")
    credentials_store.platform_grant(_FOURNISSEUR, f"user:{ancien}", daily_quota=50)
    credentials_store.platform_grant(_FOURNISSEUR, "org:41", daily_quota=500)
    monkeypatch.setattr(access, "current_org", lambda sub: 41)

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    meta = _ligne("quota")["meta"]
    assert meta["rate_limit_by"] == {"org:41": 500, f"user:{canonique}": 50}
    # Le quota servi au canonique est celui posé pour l'ancien (`user:` prime sur `org:`).
    assert _platform_grant_meta(canonique, _FOURNISSEUR, 41)["daily_quota"] == 50


def test_deux_quotas_qui_se_rencontrent_le_plus_restrictif_gagne(live):
    from oto_mcp import db
    ancien, canonique = _personnes("ancien", "canonique")
    _ligne_brute("q1", "hunter", [], {"rate_limit_by": {
        f"user:{ancien}": 50, f"user:{canonique}": 200, "org:41": 500}})
    _ligne_brute("q2", "hunter", [], {"rate_limit_by": {
        f"user:{ancien}": 80, f"user:{canonique}": 30}})
    _ligne_brute("q3", "hunter", [], {"rate_limit_by": {
        f"user:{ancien}": None, f"user:{canonique}": 40}})

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    assert _ligne("q1", "hunter")["meta"]["rate_limit_by"] == {
        f"user:{canonique}": 50, "org:41": 500}
    assert _ligne("q2", "hunter")["meta"]["rate_limit_by"] == {f"user:{canonique}": 30}
    # `None` = sans plafond : le plafond posé l'emporte.
    assert _ligne("q3", "hunter")["meta"]["rate_limit_by"] == {f"user:{canonique}": 40}
