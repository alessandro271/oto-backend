"""Un surrogate isolé venu d'un amont ne fait plus échouer l'appel d'outil.

Le cas vécu : un outil de recherche rend un dict dont un extrait porte une moitié de
paire UTF-16 (U+D83D seule) ; FastMCP lève `PydanticSerializationError: … surrogates
not allowed` en convertissant le dict en texte, DANS l'outil, et l'appel échoue en
entier. Le banc fait traverser la chaîne RÉELLE montée sur `_test_mcp()`.

⚠️ Le surrogate s'écrit `chr(0xD83D)`, jamais en littéral échappé : cf.
`tests/test_pas_de_surrogate_isole.py`.
"""
from __future__ import annotations

import asyncio
import json

import pydantic
import pydantic_core
import pytest
from _mcp_app import static_mcp as _test_mcp

from fastmcp import Client, FastMCP
from fastmcp.tools.tool import Tool
from mcp.types import CallToolResult

from oto_mcp.middleware import sans_surrogate

SEUL = chr(0xD83D)          # moitié haute d'une paire, orpheline
FFFD = "�"


def _banc(fn, *, serialiseur: bool = True, nom: str = "outil"):
    m = FastMCP("banc")
    for mw in _test_mcp().middleware:
        m.add_middleware(mw)
    m.tool(name=nom)(fn)
    if serialiseur:
        sans_surrogate.poser_le_serialiseur(m)
    return m


def _appel(m: FastMCP, nom: str = "outil"):
    async def appel():
        async with Client(m) as c:
            return await c.call_tool(nom, {}, raise_on_error=False)
    return asyncio.run(appel())


def _texte(r) -> str:
    return "".join(getattr(b, "text", "") for b in r.content)


def _transport(r) -> str:
    """Ce que fait le transport HTTP : encoder la réponse en JSON (UTF-8)."""
    return CallToolResult(content=r.content, structuredContent=r.structured_content,
                          isError=r.is_error).model_dump_json()


def _resultats_serper() -> dict:
    return {"organic": [{"title": "Résultat", "snippet": "Un extrait " + SEUL + " coupé"}]}


def test_sans_le_serialiseur_le_dict_fait_echouer_l_appel():
    """Le crash reproduit : sans le sérialiseur posé, la conversion du résultat lève
    (la frame de la trace : `Tool.convert_result` → `default_serializer`), et l'appel
    servi par la chaîne est en erreur (l'enveloppe en masque le détail)."""
    outil = Tool.from_function(_resultats_serper, name="outil")
    with pytest.raises(pydantic_core.PydanticSerializationError, match="surrogates not allowed"):
        outil.convert_result(_resultats_serper())
    assert _appel(_banc(_resultats_serper, serialiseur=False)).is_error

    outil.serializer = sans_surrogate.serialiser
    assert FFFD in outil.convert_result(_resultats_serper()).content[0].text


def test_un_dict_portant_un_surrogate_isole_est_servi():
    r = _appel(_banc(_resultats_serper))
    assert not r.is_error, _texte(r)
    servi = json.loads(_texte(r))
    assert servi["organic"][0]["snippet"] == "Un extrait " + FFFD + " coupé"
    _transport(r)  # ne lève pas


def test_un_str_portant_un_surrogate_isole_part_au_transport():
    """Un `str` ne passe pas par le sérialiseur : le middleware le nettoie au retour."""
    def brut() -> str:
        return "texte " + SEUL

    r = _appel(_banc(brut))
    assert not r.is_error
    assert _texte(r) == "texte " + FFFD
    _transport(r)


class _Fiche(pydantic.BaseModel):
    nom: str


def test_le_canal_structure_garde_est_nettoye():
    def fiche() -> _Fiche:
        return _Fiche(nom="x" + SEUL)

    r = _appel(_banc(fiche))
    assert not r.is_error, _texte(r)
    assert r.structured_content == {"nom": "x" + FFFD}
    _transport(r)


def test_une_erreur_nettoyee_reste_une_erreur():
    from fastmcp.tools.tool import ToolResult
    from mcp.types import TextContent
    erreur = ToolResult(content=[TextContent(type="text", text="amont : " + SEUL)],
                        meta={"k": 1})
    erreur.is_error = True
    r = sans_surrogate.nettoyer_resultat(erreur, "outil")
    assert r.is_error is True and r.meta == {"k": 1}
    assert r.content[0].text == "amont : " + FFFD
    propre = ToolResult(content=[TextContent(type="text", text="ok")])
    assert sans_surrogate.nettoyer_resultat(propre) is propre


def test_une_paire_complete_est_intacte_et_le_chemin_heureux_inchange():
    emoji = "\N{GRINNING FACE}"
    charge = {"a": "é " + emoji, "b": [1, 2.5, None, True]}
    from fastmcp.tools.base import default_serializer
    assert sans_surrogate.serialiser(charge) == default_serializer(charge)
    # Deux moitiés écrites en deux points de code se recomposent en leur caractère.
    haute, basse = chr(0xD83D), chr(0xDE00)
    assert sans_surrogate.nettoyer(haute + basse) == emoji
    assert sans_surrogate.nettoyer(basse) == FFFD
    propre = "déjà propre"
    assert sans_surrogate.nettoyer(propre) is propre


def test_chaque_outil_du_catalogue_porte_le_serialiseur():
    """Tous les outils servis, `oto_call` compris (il exécute la cible par `tool.run`,
    donc par le sérialiseur de la cible)."""
    m = _test_mcp()
    outils = [c for c in m.local_provider._components.values() if isinstance(c, Tool)]
    assert outils
    sans = sorted(t.name for t in outils if t.serializer is not sans_surrogate.serialiser)
    assert not sans, f"outils sans le sérialiseur : {sans[:10]}"
