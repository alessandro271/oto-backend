"""Écrire un guide sans écraser ce qu'un autre vient d'écrire (signal #1203).

Un agent d'onboarding a remplacé par `oto_guide op=write` le readme d'une org qu'un tiers
venait de poser entre deux de ses lectures : `op=write` REMPLACE, rien ne comparait.
Ce banc tient, sur base RÉELLE et par la capacité servie (`_guide_op`, droits réels — un
admin d'org et l'auteur de ses propres guides) :

- la lecture rend un `rev`, readme compris (`absent` quand il n'y a rien) ;
- l'écriture qui le renvoie passe ; celle dont la lecture est périmée est refusée en
  409 `conflict`, et rien n'est écrit ;
- une création concurrente (`absent` des deux côtés) : la seconde est refusée ;
- `op=delete` (vider un readme, supprimer un guide) est gardé pareil ;
- sans `expected_rev`, `oto_guide` écrase comme avant (le contrat n'est pas durci) ;
  `oto_guide_v2` l'exige (400 `expected_rev_required`), et il est en bêta.

Éprouvé rouge le 2026-10-23 : l'appel à `_exiger_la_version` retiré de
`set_init_guide_db` ⟹ `test_readme_ecrit_entre_lecture_et_ecriture_est_refuse` écrase.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp import db, org_store
from oto_mcp.capabilities import guides as G
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.tool_visibility import BETA_TOOLS

ADMIN, TIERS = "u-garde-admin", "u-garde-tiers"
_S = uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def org(live):
    for sub in (ADMIN, TIERS):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=sub)
    oid = org_store.create_org(f"Org gardée {_S}", created_by=ADMIN)
    org_store.add_org_member(oid, ADMIN, "org_admin")
    org_store.add_org_member(oid, TIERS, "org_admin")
    return oid


def _op(sub, oid, strict=False, **kw):
    h = G._guide_op_strict if strict else G._guide_op
    return h(ResolvedCtx(sub=sub, org_id=oid), G.GuideOpInput(**kw))


def _readme(sub, oid, **kw):
    return _op(sub, oid, scope="org", delivery="init", **kw)


def test_readme_ecrit_entre_lecture_et_ecriture_est_refuse(org):
    lu = _readme(ADMIN, org, op="read")
    assert lu["body_md"] == "" and lu["rev"] == db.REV_ABSENT
    # Un tiers pose le readme pendant que l'agent travaille.
    _readme(TIERS, org, op="write", body_md="Fiche de l'entreprise, écrite par un humain.")
    with pytest.raises(AuthzDenied) as e:
        _readme(ADMIN, org, op="write", body_md="Règles de travail.",
                expected_rev=lu["rev"])
    assert (e.value.status, e.value.code) == (409, "conflict")
    assert _readme(ADMIN, org, op="read")["body_md"].startswith("Fiche"), "rien écrit"


def test_la_lecture_fraiche_ecrit_et_dit_ce_qu_elle_remplace(org):
    lu = _readme(ADMIN, org, op="read")
    out = _readme(ADMIN, org, op="write", body_md=lu["body_md"] + "\n\nRègles.",
                  expected_rev=lu["rev"])
    assert out["previous_rev"] == lu["rev"] and out["rev"] != lu["rev"]
    assert _readme(ADMIN, org, op="read")["rev"] == out["rev"]


def test_vider_le_readme_est_garde(org):
    lu = _readme(ADMIN, org, op="read")
    with pytest.raises(AuthzDenied) as e:
        _readme(ADMIN, org, op="delete", expected_rev="0" * 16)
    assert e.value.code == "conflict"
    _readme(ADMIN, org, op="delete", expected_rev=lu["rev"])
    assert _readme(ADMIN, org, op="read")["rev"] == db.REV_ABSENT


def test_creation_concurrente_la_seconde_est_refusee(org):
    slug = f"guide-{_S}"
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="premier",
        expected_rev=db.REV_ABSENT)
    with pytest.raises(AuthzDenied) as e:
        _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="second",
            expected_rev=db.REV_ABSENT)
    assert e.value.code == "conflict"
    assert _op(ADMIN, org, op="read", scope="user", slug=slug)["body_md"] == "premier"


def test_supprimer_un_guide_modifie_depuis_la_lecture_est_refuse(org):
    slug = f"journal-{_S}"
    v1 = _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="v1")["rev"]
    _op(ADMIN, org, op="write", scope="user", slug=slug, body_md="v2")
    with pytest.raises(AuthzDenied) as e:
        _op(ADMIN, org, op="delete", scope="user", slug=slug, expected_rev=v1)
    assert e.value.code == "conflict"
    assert _op(ADMIN, org, op="read", scope="user", slug=slug)["body_md"] == "v2"


def test_sans_expected_rev_l_heritee_ecrase_comme_avant(org):
    """Le contrat servi n'est pas durci en place : le défaut historique reste."""
    _readme(TIERS, org, op="write", body_md="posé par un tiers")
    _readme(ADMIN, org, op="write", body_md="écrasé sans lecture")
    assert _readme(ADMIN, org, op="read")["body_md"] == "écrasé sans lecture"


def test_la_surface_stricte_exige_expected_rev(org):
    for op in ("write", "delete"):
        with pytest.raises(AuthzDenied) as e:
            _readme(ADMIN, org, strict=True, op=op, body_md="x" if op == "write" else None)
        assert (e.value.status, e.value.code) == (400, "expected_rev_required")
    lu = _readme(ADMIN, org, strict=True, op="read")
    out = _readme(ADMIN, org, strict=True, op="write", body_md="strict",
                  expected_rev=lu["rev"])
    assert out["body_md"] == "strict"


def test_expected_rev_refuse_sur_une_lecture(org):
    with pytest.raises(AuthzDenied) as e:
        _readme(ADMIN, org, op="read", expected_rev="x")
    assert e.value.code == "unsupported_patch_arg"


def test_la_stricte_est_beta_l_heritee_ne_l_est_pas():
    assert "oto_guide_v2" in BETA_TOOLS and "oto_guide" not in BETA_TOOLS
