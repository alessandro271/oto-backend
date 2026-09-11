"""`org.usage.connections` — les connexions de messagerie facturables, lentille MEMBRE.

Julien, 2026-09-11 : chaque connexion Unipile sur la clé Tulina coûte 100 crédits par
mois d'abonnement où elle a été connectée. Le relevé de Tulina lit donc, pour UNE org,
ses connexions sur la clé plateforme — et rien d'autre :

1. **Aucune identité** : ni `sub`, ni `email`, ni `account_id` Unipile. Une clé opaque,
   stable d'une reconnexion à l'autre, suffit à dédupliquer.
2. **Bornable** : `since` rend aussi les connexions coupées depuis — coupée le 12, une
   connexion a servi ce mois-là.
3. **Lisible par un membre, invisible des agents** : la page Credits est ouverte à tout
   membre ; ce n'est pas un outil MCP.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from oto_mcp.capabilities import org_monitoring as om
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES
from oto_mcp.db import unipile as db_unipile

CTX = ResolvedCtx(sub="membre", org_id=7)


def _fake(monkeypatch, rows):
    vu: dict = {}

    def faux(org_id, since=None):
        vu.update(org_id=org_id, since=since)
        return [dict(r) for r in rows]

    monkeypatch.setattr(om.db, "list_billable_unipile_connections", faux)
    return vu


def test_la_lentille_ne_rend_aucune_identite(monkeypatch):
    _fake(monkeypatch, [{
        "connection_key": "k1", "provider": "LINKEDIN",
        "connected_at": "2026-09-01T10:00:00Z", "disconnected_at": None,
        # ce que le store pourrait laisser fuiter, et que la projection doit ignorer
        "sub": "tulina:abc", "email": "x@y.z", "account_id": "acc_1"}])
    out = om._billable_connections(CTX, om.OrgBillableConnectionsInput(org_id=7))
    assert out == {"connections": [{"connection_key": "k1", "provider": "LINKEDIN",
                                    "connected_at": "2026-09-01T10:00:00Z",
                                    "disconnected_at": None}]}


def test_since_est_transmis_tel_quel(monkeypatch):
    vu = _fake(monkeypatch, [])
    om._billable_connections(CTX, om.OrgBillableConnectionsInput(
        org_id=7, since="2026-10-01T00:00:00+00:00"))
    assert vu == {"org_id": 7, "since": "2026-10-01T00:00:00+00:00"}


def test_sans_since_la_lecture_n_est_pas_bornee(monkeypatch):
    vu = _fake(monkeypatch, [])
    om._billable_connections(CTX, om.OrgBillableConnectionsInput(org_id=7))
    assert vu == {"org_id": 7, "since": None}


def test_un_since_qui_n_est_pas_une_date_est_refuse_avant_la_base():
    with pytest.raises(ValidationError):
        om.OrgBillableConnectionsInput(org_id=7, since="hier")


def test_la_cle_est_stable_par_connexion_et_ne_nomme_pas_le_membre():
    k = db_unipile.unipile_connection_key(7, "tulina:abc", "LINKEDIN")
    assert k == db_unipile.unipile_connection_key(7, "tulina:abc", "LINKEDIN")
    assert k != db_unipile.unipile_connection_key(8, "tulina:abc", "LINKEDIN")
    assert k != db_unipile.unipile_connection_key(7, "tulina:abc", "WHATSAPP")
    assert k != db_unipile.unipile_connection_key(7, "tulina:xyz", "LINKEDIN")
    assert len(k) == 24 and "tulina" not in k


def test_capacite_membre_en_rest_jamais_en_mcp():
    cap = next(c for c in CAPABILITIES if c.key == "org.usage.connections")
    assert cap.mcp is None
    assert cap.authz is om._MEMBER_OF
    assert (cap.rest.verb, cap.rest.path) == ("GET", "/api/orgs/{id}/usage/connections")
