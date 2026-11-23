"""Le renommage « guide » côté réponses : ce qui reste servi, ce qui est parti (#519).

Les clés de réponse doublées (`doctrine`, `doctrines`, `doctrine_id`, …) sont RETIRÉES
avec le lot D (#526, 23/11/2026) : une réponse ne sert plus que le nom d'aujourd'hui.
Ces tests gardent :

1. **Aucune clé d'hier n'est servie** — ni dans les modèles de sortie, ni dans le
   bundle d'une session sans org.
2. **L'ancien nom est encore accepté EN ENTRÉE** là où il en était un
   (`doctrine_id`, le paramètre `doctrine` de `run_start`) : ce n'était pas une clé
   de réponse.
3. **Le code d'erreur d'hier survit dans `details.legacy_code`** — un code ne se
   double pas, il n'y a qu'un champ `error`.
4. **Le nom de schéma d'hier résout encore** : un `$ref` cassé ne fait pas rater un
   modèle, il fait rater la génération ENTIÈRE du client.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import pytest

from oto_mcp import deprecations, openapi


# ── 3. L'ancien nom est encore accepté en ENTRÉE ────────────────────────────

def test_le_guide_se_lit_par_lancien_ET_le_nouveau_nom_de_parametre():
    """Le piège qu'on évite : servir `guide_id` en réponse sans l'accepter en
    entrée. L'agent recopie ce qu'il lit — il doit aboutir."""
    from oto_mcp.capabilities.orgs import instructions as oi
    champs = oi.GuideGetInput.model_fields
    assert "guide_id" in champs and "doctrine_id" in champs
    assert oi.GuideGetInput(doctrine_id=7).doctrine_id == 7
    assert oi.GuideGetInput(guide_id=7).guide_id == 7


def test_la_console_de_procedure_accepte_les_deux_noms():
    from oto_mcp.capabilities import procedure_console as pc
    champs = pc.ProcedureInput.model_fields
    assert "guide_id" in champs and "doctrine_id" in champs


@pytest.mark.asyncio
async def test_run_start_accepte_les_deux_noms_de_parametre():
    """`run_start(doctrine=…)` est cité dans des procédures écrites il y a des mois,
    que personne ne réécrira d'un coup."""
    from oto_mcp import server
    tool = await _test_mcp().get_tool("run_start")
    props = (tool.parameters or {}).get("properties", {})
    assert "guide" in props and "doctrine" in props
    requis = set((tool.parameters or {}).get("required", []))
    assert "guide" not in requis and "doctrine" not in requis


# ── 4. Ce que le SERVEUR sert vraiment : le seul nom d'aujourd'hui ──────────

_ANCIENNES = ("doctrine_id", "doctrine_version", "doctrine_ref_count", "doctrines",
              "group_doctrine", "doctrine")


def test_les_modeles_de_sortie_ne_declarent_plus_les_noms_d_hier():
    """Lot D (#526) : un `Output` qui déclarerait encore une clé d'hier promettrait
    dans `/openapi.json` une forme que le serveur n'envoie plus."""
    from oto_mcp.capabilities import agent_context, guide_library, org_monitoring
    from oto_mcp.capabilities.connectors import selection
    from oto_mcp.capabilities.datastore import activity
    from oto_mcp.capabilities.groups import guide as groupe_guide
    from oto_mcp.capabilities.orgs import instructions as oi

    modeles = (oi.GuideView, oi.InstructionsBundle, groupe_guide.GroupInstructionsBundle,
               guide_library.LibraryList, selection.MyConnectorRow, org_monitoring.RunRow,
               agent_context.AgentContextView)
    for modele in modeles:
        restees = sorted(set(modele.model_fields) & set(_ANCIENNES))
        assert not restees, f"{modele.__name__} sert encore {restees}"
    assert "guide" in activity.ActivityEntry.model_fields
    assert "doctrine" not in activity.ActivityEntry.model_fields


def test_le_bundle_vide_dune_org_absente_ne_sert_que_les_noms_d_aujourd_hui():
    import asyncio
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import instructions as oi
    out = asyncio.run(oi._get_guide(ResolvedCtx(sub="u1", org_id=None),
                                    oi.GuideGetInput()))
    assert {"guide", "guides", "group_guide"} <= set(out)
    assert not set(out) & set(_ANCIENNES)


def test_une_ligne_de_run_est_servie_sous_guide():
    """Les colonnes du journal gardent leur nom d'avant #519 : la lentille des runs
    les sert sous `guide` / `guide_version`, et sous ce nom seul."""
    from oto_mcp.capabilities import org_monitoring as om
    ligne = {"run_id": "r1", "label": "x", "doctrine": "relance", "doctrine_version": 3}
    assert om._run_servi(ligne) == {"run_id": "r1", "label": "x", "guide": "relance",
                                    "guide_version": 3}


# ── 5. Le code d'erreur d'hier ──────────────────────────────────────────────

def test_le_code_dhier_survit_dans_les_details():
    for ancien, actuel in deprecations.CODES.items():
        details = deprecations.details_avec_code_dhier(actuel)
        assert details["legacy_code"] == ancien


def test_un_code_sans_ancetre_nen_invente_pas():
    assert deprecations.details_avec_code_dhier("unknown_run") == {}
    assert deprecations.details_avec_code_dhier("unknown_run", {"a": 1}) == {"a": 1}


# ── 6. Le nom de schéma d'hier résout encore ────────────────────────────────

def test_les_noms_de_schema_dhier_resolvent():
    doc = openapi.build()
    schemas = doc["components"]["schemas"]
    for ancien, actuel in deprecations.SCHEMAS.items():
        assert actuel in schemas, f"`{actuel}` absent de components.schemas"
        assert schemas[ancien]["$ref"] == f"#/components/schemas/{actuel}", (
            f"`{ancien}` ne résout plus : un $ref cassé fait rater la génération "
            "ENTIÈRE d'un client, pas seulement le modèle visé.")
        assert schemas[ancien]["deprecated"] is True
        assert deprecations.PREAVIS_DE_RETRAIT in schemas[ancien]["description"]
        assert deprecations.REVUE.strftime("%d/%m/%Y") not in schemas[ancien]["description"]


def test_la_table_des_schemas_ne_porte_que_de_vrais_composants():
    """⚠️ Un modèle `Output` de premier niveau n'est PAS un composant : son schéma est
    inline dans la 200, et son nom n'y apparaît que comme `title`, ce qu'aucun `$ref`
    ne peut viser. Aliaser un nom pareil publierait un composant qui n'existait pas
    — et laisserait croire à un contrat là où il n'y en avait jamais eu."""
    doc = openapi.build()
    schemas = doc["components"]["schemas"]
    for actuel in deprecations.SCHEMAS.values():
        assert actuel in schemas, (
            f"`{actuel}` n'est pas un composant : cette entrée n'a rien à faire dans "
            "`deprecations.SCHEMAS`.")
