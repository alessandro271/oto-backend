"""Servir la déclaration des clés de schéma, niveau par niveau — pour qu'un front
puisse s'y confronter (oto#56), et qu'un auteur sache ce qui sera admis (01/10/2026).

`datastore/schema_keys.py` déclare ce que chacun des cinq niveaux d'un schéma admet
(tête, colonne, sous-champ, élément de liste, bloc `lifecycle`), et **qui lit chaque
clé** : le validateur, le front, ou personne (`meta`). C'est la référence du REFUS :
une clé absente de son niveau est refusée à la pose.

`keys` est le niveau COLONNE — le contrat que le dashboard confronte à ce qu'il lit
(`schema-keys-check.mjs`) ; `levels` sert les cinq.

Lecture seule, sans paramètre, identique pour tout le monde : la déclaration est un fait
de plateforme, pas une donnée d'org. `SUB_ONLY` — il faut être authentifié, rien de plus.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ...datastore import schema_keys as decl
from .._authz import SUB_ONLY
from .._types import Capability, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES


class _NoInput(BaseModel):
    pass


class SchemaKey(BaseModel):
    key: str
    #: Qui lit cet attribut : `validateur` (le backend le fait respecter), `front`
    #: (il n'existe que pour l'affichage), ou les deux. C'est l'information qui
    #: manquait : cinq attributs vivants n'étaient lus QUE par le dashboard, et une
    #: première version de l'avertissement les aurait déclarés morts.
    readers: list[str]
    what: str
    #: `true` = n'a de sens que sur une colonne, jamais sur une couche
    #: (`colonne.comment`).
    column_only: bool = False


class SchemaKeys(BaseModel):
    keys: list[SchemaKey] = Field(
        description="Tout ce qu'une colonne de schéma a le droit de porter.")
    levels: dict[str, list[SchemaKey]] = Field(description=(
        "Ce que chaque niveau admet : `head`, `field` (= `keys`), `subfield`, `item` "
        "(l'élément d'une liste, `of`) et `lifecycle`. Une clé absente de son niveau "
        "est refusée à la pose."))
    meta_max_bytes: int = Field(description=(
        "La taille maximale d'un objet `meta`, en octets de JSON compact."))


def _schema_keys(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return decl.servie()


CAPABILITIES += [
    Capability(
        key="datastore.schema_keys", handler=_schema_keys,
        Input=_NoInput, Output=SchemaKeys,
        authz=SUB_ONLY,
        mcp=None,  # contrat de FRONT : un agent lit la description de l'outil, pas ça
        rest=RestBinding("GET", "/api/datastore/schema/keys"),
        description=(
            "Every key a schema may carry, level by level (`levels`: head, field, "
            "subfield, item, lifecycle — `keys` is the field level), and WHO reads each "
            "one: the validator, the front-end, or nobody (`meta`, the free zone, "
            "carried as is, at most `meta_max_bytes`). Posting or patching a schema "
            "REFUSES a key its level does not admit, naming the path, the key and the "
            "closest known one — so a typo (`read_only` for `readonly`) can no longer "
            "disarm a guard in silence."),
    ),
]
