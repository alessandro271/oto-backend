"""Aucun résultat d'outil ne porte de surrogate isolé : il est remplacé par U+FFFD.

Un surrogate UTF-16 SEUL (U+D800–U+DFFF sans sa moitié) est un `str` Python valide,
mais l'UTF-8 refuse de l'encoder. Il arrive d'un amont : un JSON qui porte la séquence
d'échappement d'une moitié de paire (un texte coupé au milieu d'un emoji ou d'un
caractère mathématique par un service qui compte en UTF-16), que `json.loads` accepte
et rend tel quel. Rien ne casse… jusqu'à la sérialisation MCP, qui lève
`PydanticSerializationError: UnicodeEncodeError … surrogates not allowed` — et l'appel
échoue en entier pour UN caractère d'un extrait de résultat.

Deux points de passage, parce que la sérialisation casse à deux endroits :

1. **Dans l'outil**, quand FastMCP convertit la valeur rendue (un dict, une liste) en
   texte (`Tool.convert_result` → `default_serializer`). C'est AVANT tout middleware :
   aucun ne voit de résultat, seulement l'exception. Le seul crochet à cet endroit est
   le `serializer` de l'outil, que `poser_le_serialiseur` pose sur chaque outil monté
   (après le DERNIER montage, comme `un_seul_canal.retirer_les_schemas_vides`).
   Le chemin heureux est INCHANGÉ à l'octet : `default_serializer` d'abord ; ce n'est
   que s'il lève sur un surrogate que la valeur est nettoyée puis resérialisée.
2. **Au transport**, quand la réponse JSON-RPC est encodée : un outil qui rend un `str`
   (le sérialiseur n'est pas appelé pour un `str`) ou un canal structuré gardé porte le
   surrogate jusque-là. `SansSurrogateMiddleware`, le plus externe de ceux qui touchent
   au résultat, nettoie le canal texte et le canal structuré au retour.

Le remplacement passe par un aller-retour UTF-16 `surrogatepass` : une paire
légitime écrite en deux points de code se recompose en son caractère, seule une moitié
orpheline devient U+FFFD. Une chaîne propre n'est jamais recopiée.
"""
from __future__ import annotations

import functools
import logging
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import default_serializer, resolve_serialize_by_alias
from fastmcp.tools.tool import Tool, ToolResult
from mcp.types import TextContent
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)

# `pydantic_core` n'est pas une dépendance DÉCLARÉE (`tests/test_dependances_declarees.py`) :
# on passe par `pydantic`. Son `PydanticSerializationError` est un `ValueError`, reconnu
# à son message.
_SIGNATURE = "surrogates not allowed"


@functools.lru_cache(maxsize=1)
def _quelconque() -> TypeAdapter:
    return TypeAdapter(Any)


def propre(texte: str) -> bool:
    """Vrai si `texte` s'encode en UTF-8 (aucun surrogate isolé)."""
    if texte.isascii():
        return True
    try:
        texte.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def nettoyer(texte: str) -> str:
    """`texte` sans surrogate isolé (remplacé par U+FFFD) ; le même objet s'il est propre."""
    if propre(texte):
        return texte
    return texte.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sans_surrogate(valeur):
    """`valeur` (déjà JSON-able : dict, liste, str, scalaires) nettoyée en profondeur,
    clés comprises."""
    if isinstance(valeur, str):
        return nettoyer(valeur)
    if isinstance(valeur, dict):
        return {(nettoyer(k) if isinstance(k, str) else k): sans_surrogate(v) for k, v in valeur.items()}
    if isinstance(valeur, (list, tuple)):
        return [sans_surrogate(v) for v in valeur]
    return valeur


def serialiser(data) -> str:
    """Le sérialiseur de chaque outil : `default_serializer` de FastMCP, et, s'il lève sur
    un surrogate isolé, la même sérialisation de la valeur nettoyée.

    Une autre erreur de sérialisation est re-levée telle quelle : FastMCP la journalise
    et retombe sur `default_serializer`, ce qu'il faisait sans nous."""
    try:
        return default_serializer(data)
    except ValueError as e:
        if _SIGNATURE not in str(e):
            raise
    jsonable = _quelconque().dump_python(
        data, mode="json", fallback=str, by_alias=resolve_serialize_by_alias(data)
    )
    logger.warning("résultat d'outil : surrogate isolé remplacé par U+FFFD à la sérialisation")
    return default_serializer(sans_surrogate(jsonable))


def poser_le_serialiseur(instance) -> int:
    """Pose `serialiser` sur chaque outil monté ; rend leur nombre.

    À appeler après le DERNIER montage. On pose l'ATTRIBUT, pas le paramètre `serializer=`
    de la création d'un outil (déprécié par FastMCP 3, qui avertirait à chaque montage).
    ⚠️ Lit le magasin du fournisseur local (FastMCP 3.4, épinglé `<3.5`), comme
    `un_seul_canal.retirer_les_schemas_vides`. Un outil qui porte déjà son propre
    sérialiseur le garde."""
    poses = 0
    for composant in list(instance.local_provider._components.values()):
        if isinstance(composant, Tool) and composant.serializer is None:
            composant.serializer = serialiser
            poses += 1
    return poses


def _structure_propre(structure) -> bool:
    """Faux seulement sur un surrogate isolé. Une autre erreur de sérialisation n'est
    pas la nôtre : la structure passe inchangée, et le transport la dira comme avant —
    ce middleware, au-dessus de l'enveloppe d'erreur, ne lève rien de neuf."""
    try:
        _quelconque().dump_json(structure, fallback=str)
    except ValueError as e:
        return _SIGNATURE not in str(e)
    return True


def nettoyer_resultat(result, outil: str | None = None):
    """`result` (un `ToolResult`) sans surrogate isolé, dans son canal texte et son canal
    structuré ; le même objet s'il est propre."""
    if not isinstance(result, ToolResult):
        return result
    contenu = list(result.content or [])
    sale = False
    for i, bloc in enumerate(contenu):
        if isinstance(bloc, TextContent) and not propre(bloc.text):
            contenu[i] = bloc.model_copy(update={"text": nettoyer(bloc.text)})
            sale = True
    structure = result.structured_content
    if structure is not None and not _structure_propre(structure):
        structure = sans_surrogate(structure)
        sale = True
    if not sale:
        return result
    logger.warning("résultat d'outil %s : surrogate isolé remplacé par U+FFFD", outil)
    # `model_copy` : tout autre champ (`meta`, `is_error`…) reste tel quel — une erreur
    # nettoyée reste une erreur.
    return result.model_copy(update={"content": contenu, "structured_content": structure})


class SansSurrogateMiddleware(Middleware):
    """Nettoie le résultat d'un appel d'outil au retour. Cf. le module."""

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        return nettoyer_resultat(result, getattr(context.message, "name", None))
