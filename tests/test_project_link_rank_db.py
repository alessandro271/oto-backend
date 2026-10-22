"""Le rang d'un lien (`op=rank_link`) contre un vrai PostgreSQL.

Deux choses que seule la base peut dire, sur le CHEMIN SERVI (capacité `me.project`,
autz déclarée jouée sur un `RawCtx`) :

1. **L'ordre rendu.** Les liens rangés sortent d'abord, par rang ; à rang égal, le
   `label` départage ; les non rangés suivent, dans l'ordre d'avant. C'est l'`ORDER BY`
   de `list_project_links_many` qui le fait, pas une branche Python.
2. **Reposer un lien garde son rang.** `op=link` est un upsert dont le `SET` ne nomme
   pas `position` : changer le rôle d'un lien rangé ne le fait pas redescendre.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.capabilities._types import RawCtx


@pytest.fixture(scope="module")
def monde(live):
    """Une org, son admin, un projet et trois procédures liées sous les libellés
    « C », « A », « B » — dans cet ordre, pour que l'ordre rendu ne soit pas l'ordre
    d'écriture."""
    from oto_mcp import db, org_store
    org = org_store.create_org("Acme", created_by="u-admin")
    org_store.add_org_member(org, "u-admin", "org_admin")
    org_store.set_active_org("u-admin", org)
    projet = db.create_project("org", str(org), "Données", created_by="u-admin")
    monde = {"org": org, "projet": projet, "refs": {}}
    for libelle in ("C", "A", "B"):
        slug = "proc-" + uuid.uuid4().hex[:8]
        org_store.set_instruction("org", org, slug, "1. Une étape", title=slug, set_by="u-admin")
        ref = str(org_store.get_instruction("org", org, slug)["id"])
        monde["refs"][libelle] = ref
        _appel(monde, "link", target_type="procedure", target_ref=ref, label=libelle)
    return monde


def _appel(monde, op: str, **args) -> dict:
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.project")
    inp = cap.Input(op=op, project_id=monde["projet"], **args)
    return cap.handler(cap.authz(RawCtx(sub="u-admin"), inp), inp)


def _ordre(monde) -> list[tuple[str, float | None]]:
    liens = _appel(monde, "get")["links"]
    return [(l["label"], l.get("position")) for l in liens if l["target_type"] == "procedure"]


def _ranger(monde, libelle: str, **rang) -> dict:
    return _appel(monde, "rank_link", target_type="procedure",
                  target_ref=monde["refs"][libelle], **rang)


def test_ranges_d_abord_ex_aequo_par_label(monde):
    """Sans rang, l'ordre d'avant (par label). C et B au même rang : ils passent devant
    A, non rangé, et le label les départage — B avant C."""
    assert _ordre(monde) == [("A", None), ("B", None), ("C", None)]
    _ranger(monde, "C", position=1.0)
    _ranger(monde, "B", position=1.0)
    assert _ordre(monde) == [("B", 1.0), ("C", 1.0), ("A", None)]
    _ranger(monde, "A", position=0.5)
    assert _ordre(monde) == [("A", 0.5), ("B", 1.0), ("C", 1.0)]


def test_reposer_un_lien_garde_son_rang(monde):
    """Le rôle change, le rang reste : `op=link` dit `updated` sur `role` seul."""
    _ranger(monde, "C", position=7.0)
    out = _appel(monde, "link", target_type="procedure", target_ref=monde["refs"]["C"],
                 label="C", role="Relance")
    assert out["link_status"] == "updated" and out["changed_fields"] == ["role"]
    lien = next(l for l in out["links"] if l["target_ref"] == monde["refs"]["C"])
    assert (lien["role"], lien["position"]) == ("Relance", 7.0)


def test_retirer_le_rang(monde):
    _ranger(monde, "A", position=2.0)
    _ranger(monde, "A", clear_position=True)
    assert ("A", None) in _ordre(monde)
