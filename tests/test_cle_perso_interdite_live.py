"""Réserver un connecteur à une équipe : clé perso interdite par l'org (#1239), en SQL réel.

Le cas cible : un CRM réservé à l'équipe « Ventes ». L'admin pose une clé sur l'équipe et
interdit la clé perso pour ce connecteur. Un membre de Ventes passe (par la clé de
l'équipe, même s'il a une clé perso) ; un autre membre est refusé À L'APPEL, et le refus
NOMME la politique et dit à qui s'adresser — jamais « aucune clé ». La garde d'activation
ne bouge pas : le connecteur reste exposé, c'est la résolution qui refuse.

Ce n'est pas la restriction par membre retirée le 24/09 (ADR 0053 D1) : personne n'est
nommé, la politique retire le palier perso pour toute l'org.

Joué sous les deux traversées (walker et chaîne, `OTO_L7_DECIDE`) : la règle est lue des
deux côtés de la fenêtre L7.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp import access, instance_refs, session_org
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.mcp_errors import McpError

CRM = "attio"               # byo_user + byo_org, sans palier plateforme


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_cleperso_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
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


@pytest.fixture(params=["legacy", "chain"])
def traversee(request, monkeypatch):
    monkeypatch.setenv("OTO_L7_DECIDE", request.param)
    return request.param


@pytest.fixture
def monde(live, traversee, monkeypatch):
    """Org O : un admin, un membre de Ventes (V), un autre membre (A). Clé CRM posée sur
    l'équipe Ventes ; V et A ont chacun une clé perso. Le CRM est exposé (master ON)."""
    from oto_mcp import db, group_store, org_store
    from oto_mcp.connectors import activation
    u = uuid.uuid4().hex[:8]
    admin, vendeur, autre = f"admin_{u}", f"vendeur_{u}", f"autre_{u}"
    for s in (admin, vendeur, autre):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"Org {u}", created_by=admin)
    org_store.add_org_member(org, admin, "org_admin")
    org_store.add_org_member(org, vendeur)
    org_store.add_org_member(org, autre)
    ventes = group_store.create_group(org, "Ventes")
    group_store.add_group_member(ventes, vendeur)
    group_store.set_group_secret(ventes, CRM, "CLE-EQUIPE-VENTES", set_by=admin)
    db.set_member_api_key(vendeur, org, CRM, "CLE-PERSO-VENDEUR")
    db.set_member_api_key(autre, org, CRM, "CLE-PERSO-AUTRE")
    activation.set_activation(CRM, True, org_id=None, set_by=admin)
    actives = {vendeur: ventes, autre: None, admin: None}
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    monkeypatch.setattr(access, "current_group", lambda sub: actives.get(sub))
    monkeypatch.setattr(access, "project_pinned_instance", lambda p: None)
    return {"org": org, "admin": admin, "vendeur": vendeur, "autre": autre,
            "ventes": ventes, "nom": f"Org {u}"}


def _poser(m, forbidden: bool) -> dict:
    from oto_mcp.capabilities.connectors import cle_perso as cap
    return cap._set(ResolvedCtx(sub=m["admin"], org_id=m["org"]),
                    cap.PersonalKeyPolicySetInput(org_id=m["org"], name=CRM,
                                                  forbidden=forbidden))


def _resoudre(sub: str):
    return access.resolve_credential(CRM, sub=sub, emit_on_failure=False)


def test_sans_politique_chacun_resout_sa_cle_perso(monde):
    assert _resoudre(monde["autre"]).key == "CLE-PERSO-AUTRE"
    assert _resoudre(monde["vendeur"]).key == "CLE-PERSO-VENDEUR"


def test_cas_cible_ventes_passe_l_autre_est_refuse_par_son_nom(monde):
    from oto_mcp.connectors import activation_gate, cle_perso
    assert _poser(monde, True) == {"org_id": monde["org"], "connector": CRM,
                                   "forbidden": True}
    rc = _resoudre(monde["vendeur"])
    assert (rc.key, rc.mode) == ("CLE-EQUIPE-VENTES", "group")      # perso sautée
    with pytest.raises(cle_perso.ClePersoInterdite) as e:
        _resoudre(monde["autre"])
    texte = e.value.error.message
    assert "personal_key_forbidden" in texte and monde["nom"] in texte
    assert "admin of this organization" in texte
    assert "No `" not in texte                     # jamais le « aucune clé » muet
    assert e.value.error.data["code"] == "personal_key_forbidden"
    # La garde d'activation ne bouge pas : le connecteur reste exposé pour l'org.
    assert activation_gate._coupure(CRM, monde["org"], None) is None


def test_la_politique_se_lit_et_se_leve(monde):
    from oto_mcp.capabilities.connectors import cle_perso as cap
    ctx = ResolvedCtx(sub=monde["admin"], org_id=monde["org"])
    _poser(monde, True)
    lu = cap._list(ctx, cap.PersonalKeyPolicyListInput(org_id=monde["org"]))
    assert [r["connector"] for r in lu["forbidden"]] == [CRM]
    assert lu["forbidden"][0]["set_by"] == monde["admin"]
    _poser(monde, False)
    assert cap._list(ctx, cap.PersonalKeyPolicyListInput(org_id=monde["org"]))[
        "forbidden"] == []
    assert _resoudre(monde["autre"]).key == "CLE-PERSO-AUTRE"


def test_seul_un_admin_d_org_pose_ou_lit_la_politique(monde):
    from oto_mcp.capabilities import registry
    for key in ("connectors.personal_key_policy.set_org",
                "connectors.personal_key_policy.org_list"):
        cap = registry.by_key(key)
        inp = cap.Input(org_id=monde["org"], name=CRM, forbidden=True) \
            if "name" in cap.Input.model_fields else cap.Input(org_id=monde["org"])
        with pytest.raises(AuthzDenied):
            cap.authz(RawCtx(sub=monde["autre"]), inp)
        assert cap.authz(RawCtx(sub=monde["admin"]), inp).sub == monde["admin"]


def test_on_ne_branche_ni_ne_pose_une_cle_perso_interdite(monde):
    from oto_mcp.capabilities import me_credentials
    from oto_mcp.connectors import activation_gate
    _poser(monde, True)
    with pytest.raises(activation_gate.ConnecteurCoupe) as e:
        activation_gate.exiger_connectable(CRM, monde["autre"])
    assert e.value.code == "personal_key_forbidden" and monde["nom"] in e.value.message
    activation_gate.exiger_connectable(CRM, monde["autre"], perso=False)  # clé partagée
    with pytest.raises(AuthzDenied) as e:
        activation_gate.exiger_connectable_capacite(CRM, monde["autre"])
    assert (e.value.status, e.value.code) == (403, "personal_key_forbidden")
    with pytest.raises(AuthzDenied) as e:
        me_credentials._set_preparer(
            ResolvedCtx(sub=monde["autre"], org_id=monde["org"]),
            me_credentials.CredentialSetInput(provider=CRM, fields={"api_key": "x"}))
    assert (e.value.status, e.value.code) == (403, "personal_key_forbidden")


def test_l_instance_perso_designee_n_est_pas_servie(monde, monkeypatch):
    from oto_mcp.connectors import cle_perso
    _poser(monde, True)
    ref = instance_refs.parse_ref(
        instance_refs.make_member_ref(monde["org"], monde["autre"], CRM))
    monkeypatch.setattr(session_org, "current_call_instance", lambda: ref)
    with pytest.raises(cle_perso.ClePersoInterdite) as e:
        _resoudre(monde["autre"])
    assert "personal instance named by this call" in e.value.error.message


def test_un_refus_non_relatif_a_la_politique_reste_le_refus_d_avant(monde):
    """Sans politique, l'absence de clé garde son refus habituel (poser la sienne)."""
    from oto_mcp.access.resolve import CredentialUnavailable
    from oto_mcp import db
    db.clear_member_api_key(monde["autre"], monde["org"], CRM)
    with pytest.raises(CredentialUnavailable) as e:
        _resoudre(monde["autre"])
    assert "personal_key_forbidden" not in e.value.error.message
    assert isinstance(e.value, McpError)
