"""Une procédure archivée se RETROUVE : le bundle d'org la liste à qui peut la
remettre en service.

Le désarchivage (#857) existait sans point d'entrée. Une procédure archivée sort de
toutes les listes, donc on ne la remettait en service qu'en connaissant déjà son slug —
autant dire jamais, pour une personne qui l'a archivée depuis l'écran et vient la
chercher. `archived`, dans le bundle de `GET /api/me/instructions`, est ce point
d'entrée.

Contre un vrai PostgreSQL et par le CHEMIN SERVI (autz déclarée puis handler), comme
`test_droits_procedure_servis_695.py` : ce qu'on vérifie est un filtre SQL et une règle
d'autorisation, deux choses qu'un stub ne prouve pas.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp.capabilities._types import AuthzDenied, RawCtx


def _cap(key: str):
    from oto_mcp.capabilities.registry import CAPABILITIES
    return next(c for c in CAPABILITIES if c.key == key)


def _appel(key: str, sub: str, **args):
    """UN appel par le chemin servi : autz DÉCLARÉE, puis handler."""
    cap = _cap(key)
    inp = cap.Input(**args)
    out = cap.handler(cap.authz(RawCtx(sub=sub), inp), inp)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def _slugs(entrees) -> list[str]:
    return [e["slug"] for e in entrees]


@pytest.fixture(scope="module")
def monde(live):
    """Une org, son admin, une membre ; trois procédures dont deux retirées, dans un
    ordre connu — `retiree-avant` PUIS `retiree-apres`."""
    from oto_mcp import org_store
    org = org_store.create_org("Acme", created_by="u-admin")
    org_store.add_org_member(org, "u-admin", "org_admin")
    org_store.add_org_member(org, "u-membre", "org_member")
    for sub in ("u-admin", "u-membre"):
        org_store.set_active_org(sub, org)
    for slug in ("en-service", "retiree-avant", "retiree-apres"):
        _appel("org.instruction.set", "u-admin", slug=slug, body_md=f"# {slug}\n\nÉtapes.\n")
    _appel("org.instruction.archive", "u-admin", slug="retiree-avant")
    _appel("org.instruction.archive", "u-admin", slug="retiree-apres")
    return {"org": org}


def test_ladmin_retrouve_les_archivees_la_plus_recente_dabord(monde):
    from oto_mcp.capabilities.orgs import instructions as oi
    bundle = _appel("org.instruction.list", "u-admin")

    assert _slugs(bundle["instructions"]) == ["en-service"]
    assert _slugs(bundle["archived"]) == ["retiree-apres", "retiree-avant"]
    assert all(a["archived_at"] for a in bundle["archived"])

    # Ce que le handler rend doit survivre au modèle SERVI : un champ que l'OpenAPI ne
    # déclare pas est un champ qu'aucun front ne lira.
    servi = oi.InstructionsBundle.model_validate(bundle)
    assert [a.slug for a in servi.archived] == ["retiree-apres", "retiree-avant"]


def test_une_membre_recoit_une_liste_vide_et_le_geste_lui_est_refuse(monde):
    """Le vide dit vrai : il est la règle du geste que la liste sert, exécutée. La
    membre lit l'index comme tout le monde — ce n'est pas l'accès qui lui manque, c'est
    le droit de remettre en service."""
    bundle = _appel("org.instruction.list", "u-membre")
    assert _slugs(bundle["instructions"]) == ["en-service"]
    assert bundle["archived"] == []

    with pytest.raises(AuthzDenied) as refus:
        _appel("org.instruction.unarchive", "u-membre", slug="retiree-avant")
    assert refus.value.status == 403


def test_lindex_que_lit_lia_ne_les_porte_toujours_pas(monde):
    """L'invariant de l'archivage, que ce lot ne doit pas entamer : la liste des
    retirées est une surface À PART, l'index de l'agent n'en voit aucune."""
    from oto_mcp import org_store
    index = _slugs(org_store.list_instructions("org", monde["org"]))
    assert "retiree-avant" not in index and "retiree-apres" not in index


def test_sans_org_active_la_liste_est_servie_vide(monde):
    """Présente et vide, pas absente : un champ manquant se lit `undefined`."""
    bundle = _appel("org.instruction.list", "u-sans-org")
    assert bundle["org_id"] is None
    assert bundle["archived"] == []


def test_desarchiver_depuis_la_liste_la_remet_dans_lindex(monde):
    """Le chemin entier que la liste ouvre : trouvée là, remise en service, de retour
    dans `instructions` et sortie d'`archived`. En DERNIER : il modifie le monde."""
    _appel("org.instruction.unarchive", "u-admin", slug="retiree-avant")
    bundle = _appel("org.instruction.list", "u-admin")
    assert _slugs(bundle["instructions"]) == ["en-service", "retiree-avant"]
    assert _slugs(bundle["archived"]) == ["retiree-apres"]
