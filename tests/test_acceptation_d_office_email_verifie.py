"""Acceptation d'office au premier passage d'un compte : adresse vérifiée, tenant attendu.

À la naissance d'un compte, les invitations d'org et les partages d'objets en attente
pour son adresse sont acceptés d'office — à deux conditions : le jeton dit l'adresse
vérifiée (`email_verified`), et l'invitation relève du tenant du compte. Sinon rien
n'est accepté : tout reste en attente, à accepter par le lien du mail.

Vrai PostgreSQL : la file d'attente (`_PENDING`), l'adhésion, l'accès à l'objet et le
tenant de l'org vivent dans le SQL. Le mail et le magic-link sont doublés.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.auth import annuaire
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    monkeypatch.setenv("OTO_INVITE_BASE_URL", "https://front.example.test")
    monkeypatch.setattr(annuaire, "magic_url", lambda url, email, **_: url)
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    owner = f"owner_{u}"
    db.upsert_user(owner, email=f"{owner}@example.test")
    org = org_store.create_org(f"org_{u}", created_by=owner)
    org_store.add_org_member(org, owner, "org_admin")
    pid = db.create_project("org", str(org), f"p_{u}", created_by=owner)
    sub, email = f"new_{u}", f"invitee_{u}@example.test"
    org_store.create_invitation(org, email, "org_admin", owner)
    R._resources(ResolvedCtx(sub=owner, org_id=org),
                 R.ResourceInput(op="share", resource_type="project", resource_id=str(pid),
                                 audience="person", email=email, role="viewer"))
    return SimpleNamespace(owner=owner, org=org, pid=str(pid), sub=sub, email=email)


def _naissance(m, *, email_verified: bool) -> None:
    from oto_mcp import db
    db.upsert_user(m.sub, email=m.email, email_verified=email_verified)


def _rien_n_est_accepte(m) -> None:
    from oto_mcp import org_store, ownership, partage_en_attente
    assert org_store.get_org_role(m.org, m.sub) is None
    assert org_store.find_pending_invitation(m.org, m.email), "l'invitation reste en attente"
    assert not ownership.can_access(m.sub, "project", m.pid)
    assert [p["email"] for p in partage_en_attente.lister("project", m.pid)] == [m.email]


def test_adresse_verifiee_et_bon_tenant_accepte_comme_avant(monde):
    from oto_mcp import ownership, org_store
    m = monde
    _naissance(m, email_verified=True)
    assert org_store.get_org_role(m.org, m.sub) == "org_admin"
    assert org_store.find_pending_invitation(m.org, m.email) is None
    assert ownership.can_access(m.sub, "project", m.pid)


def test_adresse_non_verifiee_rien_n_est_accepte(monde):
    m = monde
    _naissance(m, email_verified=False)
    _rien_n_est_accepte(m)


def test_org_d_un_autre_tenant_rien_n_est_accepte(monde, monkeypatch):
    """L'org de l'invitation relève d'un tenant tiers (axe de la marque), le compte du
    tenant primaire ; et le partage vient d'un émetteur que l'on fait relever du tiers."""
    from oto_mcp import db, tenancy
    m = monde
    with db._connect() as conn:
        conn.execute("UPDATE orgs SET front_brand = 'tenant-tiers' WHERE id = %s", (m.org,))
    reel = tenancy.current()

    class _Registre:
        def __getattr__(self, nom):
            return getattr(reel, nom)

        def tenant_of(self, sub):
            return "tenant-tiers" if sub == m.owner else reel.tenant_of(sub)

    monkeypatch.setattr(tenancy, "current", lambda: _Registre())
    _naissance(m, email_verified=True)
    _rien_n_est_accepte(m)
