"""Ce qu'une capacité DÉCLARE sur un champ mord sur la face agent (08/09/2026, retours 796-798).

Mesuré le 08/09/2026 en production : `data_write` accepte le numéro de tableau en
entier, `data_get_schema` / `data_patch_schema` / `data_drop_column` le refusent —
« Input should be a valid string ». Trois outils du même module, deux contrats.

**Le défaut n'était dans aucun des trois : il était dans l'adaptateur qui les
fabrique.** `apply_flat_signature` ne recopiait qu'`annotation` et `description`, et
en pydantic v2 un `BeforeValidator` — comme un `Ge`, un `Le`, un `pattern` — ne vit
PAS dans `f.annotation` mais dans `f.metadata`. Toute coercition ou contrainte posée
sur un champ d'`Input` était donc **acceptée-inerte côté MCP** : elle mordait sur la
face REST (qui valide par l'`Input` lui-même) et se taisait sur la face agent. Même
mode de panne muet que #627 pour la `description`, une couche plus bas.

⚠️ **La propriété, pas la liste.** Un test qui énumérerait les trois outils connus
serait à réécrire au prochain ajout — donc abandonné, donc inutile. On mesure ici
que *n'importe quelle* déclaration voyage, et un cliquet parcourt le registre pour
que le prochain champ contraint hérite du contrôle sans qu'on y pense.

Le contrôle porte sur le **montage réel** : `_mcp_adapter.register` puis l'appel du
tool tel que FastMCP le sert. Un test posé sur `apply_flat_signature` seule dirait
que l'annotation est bien construite sans rien dire de ce que le modèle reçoit.
"""
import asyncio
from typing import Annotated, Optional

from fastmcp import FastMCP
from pydantic import BaseModel, BeforeValidator, Field

from oto_mcp.capabilities import _mcp_adapter, registry
from oto_mcp.capabilities._types import Capability, ResolvedCtx

_RECU: dict = {}


def _en_texte(v):
    """La coercition-témoin : un entier devient son texte, le reste passe intact."""
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


class _SondeInput(BaseModel):
    adresse: Annotated[str, BeforeValidator(_en_texte)]
    borne: Annotated[int, Field(ge=1, le=1000)] = 200


def _sonde_cap() -> Capability:
    def _handler(ctx, inp):
        _RECU.clear()
        _RECU.update({"adresse": inp.adresse, "borne": inp.borne})
        return {"ok": True}

    return Capability(
        key="_sonde.contrainte_servie",
        handler=_handler,
        Input=_SondeInput,
        authz=lambda raw, inp: ResolvedCtx(sub="u-sonde"),
        description="Sonde de test — jamais montée par le serveur.",
        mcp="_sonde_contrainte_servie",
    )


def _monter():
    m = FastMCP("t")
    _mcp_adapter.register(m, [_sonde_cap()])
    return m


def _schema(tool) -> Optional[dict]:
    for attr in ("parameters", "input_schema", "inputSchema"):
        s = getattr(tool, attr, None)
        if isinstance(s, dict):
            return s
    return None


def test_une_coercition_declaree_par_une_capacite_mord_sur_la_face_agent():
    """La propriété centrale : ce que la capacité déclare est APPLIQUÉ à l'appel.

    On passe un entier là où l'annotation nue dirait `str`. Sans la coercition
    déclarée, FastMCP refuse l'appel AVANT le handler (« Input should be a valid
    string ») : le refus arrive donc de l'adaptateur, pas de la capacité."""
    m = _monter()

    async def go():
        tool = await m.get_tool("_sonde_contrainte_servie")
        await tool.run({"adresse": 609})

    asyncio.run(go())
    assert _RECU["adresse"] == "609", (
        "la coercition déclarée sur l'`Input` n'a pas atteint la face agent — "
        f"le handler a reçu {_RECU.get('adresse')!r}")


def test_une_contrainte_declaree_est_publiee_dans_le_schema_servi():
    """Le pendant visible : une borne déclarée se lit dans `tools/list`.

    Une contrainte qui mord sans être publiée refuse sans avoir prévenu — l'agent
    la découvre en s'y cognant, ce qui est exactement le coût qu'on paie ici."""
    m = _monter()

    async def go():
        return _schema(await m.get_tool("_sonde_contrainte_servie")) or {}

    champ = asyncio.run(go())["properties"]["borne"]
    assert champ.get("minimum") == 1 and champ.get("maximum") == 1000, (
        f"la borne déclarée n'est pas publiée sur la face agent : {champ}")


def test_aucun_champ_du_registre_ne_perd_ses_contraintes():
    """Le cliquet, parcouru depuis le REGISTRE — donc jamais une liste à tenir.

    On lit l'annotation que l'adaptateur remet à FastMCP (`_make_tool`, le chemin
    réel du montage) : c'est elle dont le test précédent prouve, bout en bout, que
    son contenu mord. Chaque objet de `f.metadata` doit s'y retrouver.

    Mesuré le 08/09/2026 avant correctif : 5 champs de 5 capacités montées le
    perdaient (`data_get_schema`, `data_patch_schema`, `data_drop_column`,
    `oto_admin_legal_proof`, `oto_resource_v2`) — et 27 champs de 23 capacités
    REST-only l'auraient hérité en silence le jour où elles gagneraient un `mcp=`."""
    from typing import get_args, get_origin

    perdus = []
    for cap in registry.caps_with_mcp():
        if not cap.is_exposed():          # feature flag off (dark launch) → non montée
            continue
        fn = _mcp_adapter._make_tool(cap)
        for nom, f in cap.Input.model_fields.items():
            if not f.metadata:
                continue
            ann = fn.__annotations__[nom]
            portes = get_args(ann)[1:] if get_origin(ann) is not None else ()
            for m in f.metadata:
                if m not in portes:
                    perdus.append(f"{cap.mcp}.{nom} :: {m!r}")
    assert not perdus, (
        "contraintes déclarées mais NON transportées sur la face agent :\n  "
        + "\n  ".join(perdus))
