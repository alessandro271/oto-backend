"""Rejoindre l'org perso de quelqu'un ne le laisse pas SANS org perso (29/09/2026).

Une org perso est mono-membre : `add_org_member` remet `personal_of` à NULL dès qu'un
2e membre la rejoint. Son ex-propriétaire n'avait alors plus d'org perso jusqu'au
prochain boot (`backfill_personal_orgs`). Or les projets privés (`owner_type='user'`)
ne se listent que dans l'org perso (`ownership.perso_de_la_liste`) : vécu en prod,
« tous mes projets perso ont disparu » à la minute où un coéquipier a accepté son
invitation.

Chaque entrée d'un 2e membre est jouée : lien d'invitation, réconciliation de signup
(sans clic), ajout par un admin (`org.member.add` / `set_role`). Vrai PostgreSQL : la
cause tient à l'ordre transaction / lecture de `personal_of`, qu'une doublure ne
montrerait pas.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _perso(sub: str) -> int:
    from oto_mcp import org_store
    from oto_mcp.db import upsert_user
    upsert_user(sub)
    return org_store.ensure_personal_org(sub, f"{sub}@x.tld", sub)


def _via_token(org_id, sub):
    from oto_mcp import org_store
    _id, token = org_store.create_invitation(org_id, f"{sub}@x.tld", "org_member",
                                             "u-perso-emetteur")
    return org_store.accept_invitation(token, sub)


def _via_signup(org_id, sub):
    from oto_mcp import org_store
    org_store.create_invitation(org_id, f"{sub}@x.tld", "org_member", "u-perso-emetteur")
    return org_store.reconcile_signup_with_invitation(sub, f"{sub}@x.tld")


def _via_admin(org_id, sub):
    from oto_mcp.capabilities.orgs import members as cap
    from oto_mcp.db import upsert_user
    upsert_user(sub)
    return cap._write_member_role(org_id, sub, "org_member", require_member=False,
                                  actor="u-perso-admin")


ENTREES = pytest.mark.parametrize(
    "rejoint", [_via_token, _via_signup, _via_admin],
    ids=["lien mail", "signup sans clic", "ajout par un admin"])


@ENTREES
def test_lex_proprietaire_garde_une_org_perso(live, rejoint):
    from oto_mcp import org_store, ownership
    proprio = f"u-perso-proprio-{rejoint.__name__}"
    perso = _perso(proprio)
    assert org_store.get_personal_org(proprio) == perso

    rejoint(perso, f"u-perso-invite-{rejoint.__name__}")

    # L'org rejointe n'est plus perso (comportement voulu, inchangé)…
    assert not org_store.is_personal_org(perso)
    # … mais son ex-propriétaire en a une autre, TOUT DE SUITE.
    neuve = org_store.get_personal_org(proprio)
    assert neuve is not None, (
        "l'ex-propriétaire n'a plus d'org perso : ses projets privés ne se listent "
        "plus nulle part jusqu'au prochain boot")
    assert neuve != perso
    # Ses projets privés s'y listent ; pas dans l'org devenue partagée (53d1446c).
    assert ownership.perso_de_la_liste(proprio, neuve) == [("user", proprio)]
    assert ownership.perso_de_la_liste(proprio, perso) == []
    # Sa maison n'a pas bougé : il reste dans l'org qu'il vient d'ouvrir à un coéquipier.
    assert org_store.get_active_org(proprio) == perso


def test_idempotent_une_seule_org_perso_rendue(live):
    from oto_mcp import org_store
    proprio = "u-perso-idem"
    perso = _perso(proprio)
    _via_admin(perso, "u-perso-idem-invite")
    neuve = org_store.get_personal_org(proprio)

    assert org_store.ensure_members_personal_orgs(perso) == []
    _via_admin(perso, "u-perso-idem-invite")          # ré-écriture de rôle
    assert org_store.get_personal_org(proprio) == neuve
