"""L'analytique Pennylane côté outils : les axes, les catégories, et catégoriser.

Trois objets portent des catégories analytiques (facture client, facture d'achat,
ligne d'écriture) ; chacun gagne `op="categories"` (lire) et `op="categorize"`
(REMPLACER). Ce qui casse en silence côté agent : un `op` routé vers le mauvais objet
(on catégorise la facture d'achat 12 au lieu de la facture client 12), une liste
mal formée qui part quand même, et `[]` — qui RETIRE tout — avalé comme « rien ».
"""
import asyncio
import importlib
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    """`_client()` importe `PennylaneClient` depuis le PACKAGE à chaque appel :
    c'est l'attribut du package qu'on remplace."""
    import oto.tools.pennylane as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def _tool(nom: str, module: str):
    from fastmcp import FastMCP

    m = FastMCP("t")
    importlib.import_module(f"oto_mcp.tools.{module}").register(m)
    return asyncio.run(m.get_tool(nom)).fn


# L'outil, son module, l'argument qui désigne l'objet, et le nom de l'objet côté client.
OBJETS = [
    ("pennylane_invoice", "pennylane", "invoice_id", "customer_invoice"),
    ("pennylane_supplier_invoice", "pennylane_achats", "invoice_id", "supplier_invoice"),
    ("pennylane_ledger_entry", "pennylane_ledger", "line_id", "ledger_entry_line"),
]


@pytest.mark.parametrize("outil,module,arg,cible", OBJETS)
def test_lire_les_categories_vise_le_bon_objet(client, outil, module, arg, cible):
    client.get_categories_of.return_value = [{"id": 3, "weight": "1.0"}]
    out = _tool(outil, module)(op="categories", **{arg: 12})
    client.get_categories_of.assert_called_once_with(cible, 12)
    assert out == [{"id": 3, "weight": "1.0"}]


@pytest.mark.parametrize("outil,module,arg,cible", OBJETS)
def test_categoriser_remplace_sur_le_bon_objet(client, outil, module, arg, cible):
    poses = [{"id": 3, "weight": "0.5"}, {"id": 4, "weight": "0.5"}]
    client.categorize.return_value = poses
    assert _tool(outil, module)(op="categorize", categories=poses, **{arg: 12}) == poses
    client.categorize.assert_called_once_with(cible, 12, poses)


@pytest.mark.parametrize("outil,module,arg,cible", OBJETS)
def test_une_liste_vide_retire_tout_et_part(client, outil, module, arg, cible):
    """`[]` est un geste (retirer toutes les catégories), pas une absence."""
    _tool(outil, module)(op="categorize", categories=[], **{arg: 12})
    client.categorize.assert_called_once_with(cible, 12, [])


@pytest.mark.parametrize("outil,module,arg,cible", OBJETS)
def test_sans_liste_ni_objet_rien_ne_part(client, outil, module, arg, cible):
    with pytest.raises(McpError, match="categories"):
        _tool(outil, module)(op="categorize", **{arg: 12})
    with pytest.raises(McpError, match=arg):
        _tool(outil, module)(op="categories")
    client.categorize.assert_not_called()
    client.get_categories_of.assert_not_called()


def test_un_corps_mal_forme_est_refuse_en_clair(client):
    """Le client refuse AVANT l'appel (`ValueError`) : l'agent doit lire pourquoi, pas
    une trace Python."""
    client.categorize.side_effect = ValueError("weight: 2 is outside 0..1")
    with pytest.raises(McpError, match="op='categorize': weight: 2 is outside 0..1"):
        _tool("pennylane_invoice", "pennylane")(
            op="categorize", invoice_id=12, categories=[{"id": 3, "weight": 2}])


def test_le_referentiel_sert_les_axes_et_les_categories(client):
    ref = _tool("pennylane_ref", "pennylane")
    ref(kind="category_groups")
    client.list_category_groups.assert_called_once_with(max_pages=None)
    ref(kind="categories")
    client.get_categories.assert_called_with(None, max_pages=None)
    ref(kind="categories", category_group_id=7, max_pages=2)
    client.get_categories.assert_called_with(7, max_pages=2)


def test_la_description_dit_que_la_pose_remplace():
    from fastmcp import FastMCP
    from oto_mcp.tools import pennylane as P

    m = FastMCP("t")
    P.register(m)
    doc = asyncio.run(m.get_tool("pennylane_invoice")).description or ""
    assert "REPLACES" in doc and "`[]` removes" in doc
