"""L'accusé d'une écriture PERSONNELLE dit ce que cette portée implique, et le bundle
de session montre enfin ce qu'on a écrit chez soi.

**Ce qui s'est passé le 08/09/2026.** Un agent écrit deux procédures avec
`oto_procedure op=set` sans `scope`. Depuis l'ADR 0068 (04/09) elles partent au palier
PERSONNEL. Son accusé disait bien `scope: "user"` — discrètement, à côté d'un
`_org: {id, name}` bien visible ajouté par l'adaptateur MCP. Il a conclu qu'il avait
écrit pour son org. Puis la session suivante n'a rien trouvé : ni l'index poussé au
`tools/list` ni le bundle de début de session ne lisaient le palier personnel. Deux
campagnes réécrites.

Trois faits sont vérifiés ici, sur le chemin SERVI (capacité + autz déclarée) et
contre un vrai PostgreSQL — ce sont des clés de propriété et un cumul de requêtes,
qu'aucun double ne prouve :

1. l'accusé NOMME l'implication de la portée privée, et porte l'identifiant stable ;
2. le bundle de session cumule le palier personnel ;
3. la promesse de l'accusé est vraie : un autre compte de la MÊME org ne la voit pas.

L'absence d'écho `_org` sur une réponse personnelle est vérifiée à part, dans
`test_index_paliers_perso.py` (elle se joue dans l'adaptateur, sans base).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from oto_mcp.capabilities._types import RawCtx

_CORPS = ("> **Self-improvement digest** — jamais déroulée.\n\n"
          "# Ma passe\n\n```\n[Début] --> [Fin]\n```\n\nÉtapes.\n")


@pytest.fixture(scope="module")
def monde(pg_module_dsn):
    """Une base neuve pour ce module, une org, son admin et un simple membre — le
    second compte est indispensable : sans lui, « privée » n'est pas vérifiable."""
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp import org_store
        from oto_mcp.db import init_db
        init_db()
        org = org_store.create_org("Acme", created_by="u-admin")
        org_store.add_org_member(org, "u-admin", "org_admin")
        org_store.add_org_member(org, "u-membre", "org_member")
        org_store.set_active_org("u-admin", org)
        org_store.set_active_org("u-membre", org)
        yield {"org": org}
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _appel(sub: str, **args):
    """UN appel d'`oto_procedure` par le chemin servi : autz DÉCLARÉE puis handler."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "org.procedure.console")
    inp = cap.Input(**args)
    out = cap.handler(cap.authz(RawCtx(sub=sub), inp), inp)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


# ── 1. L'accusé dit l'implication, pas seulement le fait ───────────────────

def test_lecriture_sans_scope_dit_ce_que_privee_implique(monde):
    ack = _appel("u-membre", op="set", slug="ma-passe", body_md=_CORPS,
                 title="Ma passe", description="Perso.")
    assert ack["ok"] and ack["scope"] == "user" and ack["user_id"] == "u-membre"
    assert "org_id" not in ack                      # jamais deux clés de portée

    note = ack["scope_note"]
    assert "PRIVÉE" in note                          # le fait, en clair
    assert "aucun autre compte de ton org" in note   # ce qu'il implique
    assert "scope='org'" in note                     # et le geste qui le change


def test_lecriture_dorg_ne_porte_pas_cette_note(monde):
    """Une note servie à chaque écriture d'org serait du bruit — et un texte servi
    qu'on apprend à ignorer est un texte perdu quand il compte."""
    ack = _appel("u-admin", op="set", scope="org", slug="socle", body_md=_CORPS,
                 title="Socle")
    assert ack["scope"] == "org" and ack["org_id"] == monde["org"]
    assert "scope_note" not in ack


def test_laccuse_porte_lidentifiant_stable(monde):
    """Deux paliers portent le même slug en production (mesuré le 09/09/2026 : sur
    deux orgs, un même slug de campagne). Le slug ne désigne donc pas une ligne ;
    `guide_id` si — et c'est par lui qu'on relit sans ambiguïté."""
    perso = _appel("u-membre", op="set", slug="meme-slug", body_md=_CORPS, title="Perso")
    org = _appel("u-admin", op="set", scope="org", slug="meme-slug", body_md=_CORPS,
                 title="Org")
    assert isinstance(perso["guide_id"], int) and isinstance(org["guide_id"], int)
    assert perso["guide_id"] != org["guide_id"]

    relu = _appel("u-membre", op="get", guide_id=perso["guide_id"])
    assert relu["scope"] == "user" and relu["title"] == "Perso"


# ── 2. Le bundle de session montre ce qu'on a écrit chez soi ───────────────

def _index(sub: str) -> dict:
    """`{slug: scope}` du bundle servi au début d'une session (slug omis)."""
    return {g["slug"]: g["scope"] for g in _appel(sub, op="get")["guides"]}


def test_le_bundle_de_session_cumule_le_palier_personnel(monde):
    _appel("u-membre", op="set", slug="dans-le-bundle", body_md=_CORPS, title="P")
    _appel("u-admin", op="set", scope="org", slug="celle-de-lorg", body_md=_CORPS,
           title="O")
    index = _index("u-membre")
    assert index.get("dans-le-bundle") == "user"     # ce qu'il n'y avait pas
    assert index.get("celle-de-lorg") == "org"       # ce qui y était déjà


# ── 3. La promesse de l'accusé est vraie ───────────────────────────────────

def test_un_autre_compte_de_la_meme_org_ne_voit_pas_la_procedure_privee(monde):
    """Sans cette assertion, ① et ② pourraient être satisfaits par un cumul qui a
    OUVERT le palier perso à toute l'org — l'inverse exact de ce que la note promet."""
    _appel("u-membre", op="set", slug="rien-que-la-mienne", body_md=_CORPS, title="P")
    assert "rien-que-la-mienne" in _index("u-membre")
    assert "rien-que-la-mienne" not in _index("u-admin")
    catalogue = {g["slug"] for g in _appel("u-admin", op="list")["guides"]}
    assert "rien-que-la-mienne" not in catalogue
