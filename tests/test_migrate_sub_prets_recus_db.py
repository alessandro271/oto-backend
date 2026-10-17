"""Un prêt fait à une personne la SUIT quand ses comptes fusionnent (#1251) — exercé sur
le merge réel (`db.migrate_sub`), en SQL réel.

Un prêt nominatif est une entrée `user:<sub>` du `share_side` de la ligne du PRÊTEUR
(ADR 0044), et, pour un compte Google ou Microsoft, la clé de la même cible dans
`share_services`. La fusion laissait `user:<ancien sub>` : le compte prêté par son nom
cessait de servir l'emprunteur fusionné, sans refus qui l'explique. Trois cas :
  1. bout en bout : prêt à A, fusion A → A', l'emprunteur A' est servi en NOMMANT le
     compte (`_instance=`), et le journal des prêts garde la cible d'origine ;
  2. le canonique figurait déjà dans le prêt : pas de doublon, services réunis ;
  3. les entrées `group:` et `org:` ne bougent pas, une ligne qui ne nomme pas
     l'ancien n'est pas touchée.

`live` (conftest) : base neuve du module, bootée par le vrai chemin.
"""
from __future__ import annotations

import json
import uuid

import pytest


@pytest.fixture
def cle(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "6" * 64)


def _personnes(*prefixes):
    from oto_mcp import db
    u = uuid.uuid4().hex[:8]
    subs = [f"{p}_{u}" for p in prefixes]
    for sub in subs:
        db.upsert_user(sub, email=f"{sub}@exemple.test")
    return subs


def _partage(entity_id, connector, account=""):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT share_side, share_services, version FROM connector_credentials "
            "WHERE entity_type = 'member' AND entity_id = %s AND connector = %s "
            "AND account = %s", (entity_id, connector, account)).fetchone()


def _ligne_brute(entity_id, connector, account, side, services):
    """Une ligne de prêt posée telle quelle : on teste ce que le STOCKAGE porte après la
    fusion, le secret n'est jamais lu (la ligne n'appartient pas au compte fusionné)."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "INSERT INTO connector_credentials (entity_type, entity_id, connector, "
            "account, secret_enc, share_side, share_services) VALUES "
            "('member', %s, %s, %s, 'chiffre-factice', %s::jsonb, %s::jsonb)",
            (entity_id, connector, account, json.dumps(side), json.dumps(services)))


def test_le_pret_suit_l_emprunteur_fusionne_qui_le_nomme(live, cle, monkeypatch):
    from oto_mcp import access, credentials_store, db, instance_refs, org_store
    from oto_mcp.db import loan_events
    from oto_mcp.mcp_errors import McpError
    preteur, ancien, canonique = _personnes("preteur", "ancien", "canonique")
    org = org_store.create_org(f"org_{preteur}", created_by=preteur)
    org_store.add_org_member(org, preteur)
    db.set_member_api_key(preteur, org, "hunter", "sk_" + preteur)
    eid = credentials_store.member_id(org, preteur)
    assert credentials_store.set_instance_sharing(
        credentials_store.MEMBER, eid, "hunter", "", share_side=[f"user:{ancien}"],
        journal=[{"actor_sub": preteur, "org_id": org, "target": f"user:{ancien}",
                  "action": loan_events.LEND}])
    ref = instance_refs.parse_ref(instance_refs.make_member_ref(org, preteur, "hunter"))
    # L'org co-posée est celle de l'APPELANT (hors de ce qu'on prouve ici).
    monkeypatch.setattr(access, "current_org", lambda sub: 777)
    assert access.guard_instance_access(ancien, ref) == 777
    with pytest.raises(McpError):           # contrefactuel : pas encore à lui
        access.guard_instance_access(canonique, ref)

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    # Servi en nommant le compte, sous son identifiant canonique.
    assert access.guard_instance_access(canonique, ref) == 777
    assert _partage(eid, "hunter")["share_side"] == [f"user:{canonique}"]
    # La projection « partagé avec moi » le trouve sous le canonique, plus sous l'ancien.
    assert [r["entity_id"] for r in
            credentials_store.list_shared_with([f"user:{canonique}"])] == [eid]
    assert credentials_store.list_shared_with([f"user:{ancien}"]) == []
    # La ligne reste déchiffrable : `share_side` est hors AAD.
    assert credentials_store.get_credential(credentials_store.MEMBER, eid,
                                            "hunter") == "sk_" + preteur
    # Le journal est un journal : la cible reste celle qui a été nommée au geste.
    assert [e["target"] for e in loan_events.pour_la_ligne(
        credentials_store.MEMBER, eid, "hunter")] == [f"user:{ancien}"]


def test_le_canonique_deja_present_ne_se_double_pas_et_garde_tous_ses_services(live):
    from oto_mcp import db
    preteur, ancien, canonique = _personnes("preteur", "ancien", "canonique")
    eid = f"41:{preteur}"
    _ligne_brute(eid, "google", "a@exemple.test",
                 [f"user:{ancien}", "group:3", f"user:{canonique}", "org:41"],
                 {f"user:{ancien}": ["gmail", "google_ads"],
                  f"user:{canonique}": ["google_ads", "drive"],
                  "group:3": ["gmail"], "org:41": ["drive"]})

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    ligne = _partage(eid, "google", "a@exemple.test")
    assert ligne["share_side"] == [f"user:{canonique}", "group:3", "org:41"]
    assert ligne["share_services"] == {f"user:{canonique}": ["google_ads", "drive", "gmail"],
                                       "group:3": ["gmail"], "org:41": ["drive"]}


def test_les_entrees_d_equipe_et_d_org_restent_intactes(live):
    from oto_mcp import db
    preteur, ancien, canonique, tiers = _personnes("preteur", "ancien", "canonique",
                                                   "tiers")
    visee, autre = f"41:{preteur}", f"42:{preteur}"
    _ligne_brute(visee, "hunter", "", ["group:3", f"user:{ancien}", "org:41",
                                       f"user:{tiers}"], {})
    _ligne_brute(autre, "hunter", "", ["group:3", "org:42", f"user:{tiers}"], {})
    version_autre = _partage(autre, "hunter")["version"]

    assert db.migrate_sub(ancien, canonique, operator_source="test") is True

    assert _partage(visee, "hunter")["share_side"] == [
        "group:3", f"user:{canonique}", "org:41", f"user:{tiers}"]
    # Une ligne qui ne nomme pas l'ancien n'est même pas réécrite.
    intacte = _partage(autre, "hunter")
    assert intacte["share_side"] == ["group:3", "org:42", f"user:{tiers}"]
    assert intacte["version"] == version_autre
