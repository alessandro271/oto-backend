"""Une écriture ne change pas la LIVRAISON d'une couche qu'elle ne nomme pas.

**Le défaut, mesuré le 09/09/2026.** L'identifiant public d'une couche de contexte
est DÉRIVÉ de `(scope, owner, slug)` et ignore `delivery` (`db/guides.py::_public_id_sql`),
alors que toutes les lectures filtrent dessus. Les deux familles se partagent donc la
même ligne :

- `PUT /api/orgs/{id}/guides/org/readme` (ou `oto_guide op=write scope=org slug=readme`)
  tombait sur la MÊME ligne que le readme `init` de l'org — celui qui est concaténé au
  début de CHAQUE session de cette org. Il en remplaçait le corps, laissait `delivery`
  à `init`, et rendait **200**. La prose reçue par toutes les sessions de l'org était
  détruite, et le guide écrit restait introuvable en lecture (qui exige
  `delivery='on-demand'`) : une écriture réussie, invisible, et destructrice.
- le sens inverse est aussi ouvert : écrire le readme `init` d'un périmètre dont le
  slug canonique porte déjà un guide à charger en remplacerait le corps.

**Contre un vrai PostgreSQL.** Le refus n'est pas une branche Python : c'est le `WHERE`
d'un `ON CONFLICT ... DO UPDATE`. Un double rendrait ce qu'on lui a appris à rendre, et
c'est précisément l'arbitrage du moteur qu'on vérifie ici. Les gestes passent par la
capacité `me.guide` avec sa règle d'autz déclarée — le chemin servi, pas le store.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from oto_mcp.capabilities._types import AuthzDenied, RawCtx

_INIT_ORG = "# Socle de l'org\n\nCe texte part dans chaque session."
_INIT_USER = "Mes préférences de travail."


@pytest.fixture(scope="module")
def monde(pg_module_dsn):
    """Une base neuve pour ce module (cf. `conftest.pg_module_dsn`), bootée par le vrai
    `init_db`, avec une org et son admin."""
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp import org_store
        from oto_mcp.db import init_db
        init_db()
        org = org_store.create_org("Acme", created_by="u-admin")
        org_store.add_org_member(org, "u-admin", "org_admin")
        org_store.set_active_org("u-admin", org)
        yield {"org": org}
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _guide(sub: str, **args):
    """UN appel d'`oto_guide` par le chemin servi : autz DÉCLARÉE puis handler."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.guide")
    inp = cap.Input(**args)
    out = cap.handler(cap.authz(RawCtx(sub=sub), inp), inp)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


# ── 1. Le témoin : le readme injecté d'une org ne se fait pas remplacer ─────

def test_un_guide_a_charger_ne_remplace_pas_le_readme_injecte_dune_org(monde):
    org = str(monde["org"])
    _guide("u-admin", op="write", scope="org", delivery="init",
           owner_id=org, body_md=_INIT_ORG)

    with pytest.raises(AuthzDenied) as refus:
        _guide("u-admin", op="write", scope="org", owner_id=org, slug="readme",
               body_md="Un how-to sans rapport.", title="How-to")

    assert refus.value.status == 409 and refus.value.code == "delivery_conflict"
    # ⚠️ Le refus NOMME sa destination : ce qui occupe la place, et les deux gestes
    # possibles. Un code nu laisserait l'agent réessayer le même appel.
    message = str(refus.value)
    assert "readme" in message and "delivery='init'" in message
    assert "choisis un autre slug" in message

    # Rien n'a bougé : c'est la moitié du test qui compte vraiment.
    relu = _guide("u-admin", op="read", scope="org", delivery="init", owner_id=org)
    assert relu["body_md"] == _INIT_ORG

    # Et le guide refusé n'existe nulle part : pas de ligne fantôme.
    with pytest.raises(AuthzDenied) as absent:
        _guide("u-admin", op="read", scope="org", slug="readme")
    assert absent.value.status == 404


def test_le_meme_refus_au_palier_personnel(monde):
    _guide("u-seul", op="write", scope="user", delivery="init", body_md=_INIT_USER)
    with pytest.raises(AuthzDenied) as refus:
        _guide("u-seul", op="write", scope="user", slug="readme", body_md="autre chose")
    assert refus.value.status == 409
    assert _guide("u-seul", op="read", scope="user",
                  delivery="init")["body_md"] == _INIT_USER


# ── 2. Le sens inverse, tout aussi ouvert ──────────────────────────────────

def test_un_readme_injecte_ne_remplace_pas_un_guide_a_charger(monde):
    """`u-envers` n'a pas de readme : son premier `readme` est un guide À CHARGER.
    Écrire son readme injecté ensuite en écrasait le corps, sans un mot."""
    _guide("u-envers", op="write", scope="user", slug="readme",
           body_md="Mon how-to nommé readme.", title="How-to")

    with pytest.raises(AuthzDenied) as refus:
        _guide("u-envers", op="write", scope="user", delivery="init",
               body_md="Mon socle perso.")
    assert refus.value.status == 409 and refus.value.code == "delivery_conflict"
    assert "on-demand" in str(refus.value) and "op='delete'" in str(refus.value)

    assert _guide("u-envers", op="read", scope="user",
                  slug="readme")["body_md"] == "Mon how-to nommé readme."


# ── 3. La garde ne ferme QUE ça — les écritures normales passent ───────────

def test_les_ecritures_normales_passent_toujours(monde):
    """Sans ce test, une garde qui refuse TOUT passerait les deux premiers."""
    org = str(monde["org"])

    # Création puis MISE À JOUR d'un guide à charger (le chemin `DO UPDATE`, celui que
    # le `WHERE` aurait pu fermer par erreur).
    _guide("u-admin", op="write", scope="org", owner_id=org, slug="bulk",
           body_md="v1", title="Bulk")
    _guide("u-admin", op="write", scope="org", owner_id=org, slug="bulk",
           body_md="v2", title="Bulk")
    assert _guide("u-admin", op="read", scope="org", slug="bulk")["body_md"] == "v2"

    # Création puis mise à jour d'un readme injecté, y compris son EFFACEMENT (corps
    # vide : « la note se retire comme elle s'écrit »).
    _guide("u-admin", op="write", scope="org", delivery="init", owner_id=org,
           body_md="socle v2")
    assert _guide("u-admin", op="read", scope="org", delivery="init",
                  owner_id=org)["body_md"] == "socle v2"
    _guide("u-admin", op="write", scope="org", delivery="init", owner_id=org, body_md="")
    assert _guide("u-admin", op="read", scope="org", delivery="init",
                  owner_id=org)["body_md"] == ""

    # Un guide à charger nommé `readme` reste possible là où AUCUN readme n'a été écrit.
    _guide("u-vierge", op="write", scope="user", slug="readme", body_md="légitime")
    assert _guide("u-vierge", op="read", scope="user",
                  slug="readme")["body_md"] == "légitime"
