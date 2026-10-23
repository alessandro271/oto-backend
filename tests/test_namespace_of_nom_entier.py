"""`namespace_of` prend le nom ENTIER pour candidat (`docs/waterfall.md`) — verrouille que
ce changement ne déplace AUCUN outil monté hors des connecteurs qui ne servent que `<ns>` :
la résolution de chaque outil est rejouée avec la règle d'avant (dernier token exclu), et
seuls les outils dont le nom entier EST un connecteur (`waterfall_email`…) changent de
namespace — pour le leur.

Un outil neuf dont le nom entier serait celui d'un AUTRE connecteur changerait de gate en
silence : ce test le nomme."""
import asyncio

from fastmcp import FastMCP

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

def _avant(name: str) -> str:
    """La règle d'avant, recopiée ici pour comparer : le plus long préfixe déclaré, le
    nom entier et le 1er token exclus ; à défaut, le 1er token."""
    if "_" not in name:
        return name
    parts = name.split("_")
    for i in range(len(parts) - 1, 1, -1):
        candidate = "_".join(parts[:i])
        if providers.connector_for_namespace(candidate) is not None:
            return candidate
    return parts[0]


def test_seuls_les_connecteurs_a_nom_entier_changent_de_namespace():
    from oto_mcp.tools import register_all
    m = FastMCP("t")
    register_all(m)
    noms = {t.name for t in asyncio.run(m._list_tools())}
    # Les outils `<ns>` d'un connecteur multi-token qui ne sert que `<ns>` / `<ns>_act`.
    noms_entiers = {n for n in noms
                    if "_" in n and providers.connector_for_namespace(n) is not None}
    changes = {n: namespace_of(n) for n in noms if _avant(n) != namespace_of(n)}
    assert set(changes) == noms_entiers, sorted(set(changes) ^ noms_entiers)
    for n in noms_entiers:
        assert changes[n] == n and namespace_of(f"{n}_act") == n
