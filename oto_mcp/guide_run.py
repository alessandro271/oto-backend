"""Runs — pile de runs en état de session (ADR 0017, barreau 1-2).

Un **run** = un déroulé borné (`run_start` → `run_finish`) : soit l'exécution d'une
guide nommé (champ `guide`, servi aussi sous son nom d'hier `doctrine` jusqu'au
29/10/2026), soit un run one-shot/ad-hoc (sans lui).
Le `run_id` actif vit dans l'**état de session FastMCP** (session-scopé, TTL natif),
sous forme de **pile** (runs imbriqués : un run peut en démarrer un autre).

- `run_start` (tools/guide_run.py) **pousse** un run.
- Le sink calllog (`server._calllog_sink`) lit le run **actif** (sommet de pile) et
  **stampe** chaque `tool_call` avec — corrélation côté serveur, l'agent ne thread rien.
- `run_finish` **dépile** (par run_id, robuste à l'imbrication).
- Une session NEUVE d'un agent hébergé (jeton de délégation) **reprend** le run que
  son jeton tient ouvert (`reprendre_run_du_jeton`, lu du journal) : la pile ne
  survit pas à un relais relancé, le travail si.

State-only : aucune table. La trace durable se dérive de `tool_calls` (run_id) et,
plus tard, des facts promus (barreau 4).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Clé d'état de session (préfixée oto pour ne pas collisionner avec d'autres états).
_STACK_KEY = "oto_guide_runs"


def new_run_id() -> str:
    return uuid.uuid4().hex


async def _read_stack(ctx: Any) -> list[dict]:
    try:
        stack = await ctx.get_state(_STACK_KEY)
    # noqa: SILENT — pile de guide illisible ⇒ vide, le guide reste servi
    except Exception:
        return []
    return list(stack) if isinstance(stack, list) else []


async def push_run(ctx: Any, run_id: str, label: str, guide: Optional[str] = None) -> None:
    stack = await _read_stack(ctx)
    stack.append({"run_id": run_id, "label": label, "guide": guide})
    await ctx.set_state(_STACK_KEY, stack)


async def pop_run(ctx: Any, run_id: str) -> Optional[dict]:
    """Retire le run `run_id` (n'importe où dans la pile, robuste à l'imbrication).
    Renvoie l'entrée retirée ou None si absente."""
    stack = await _read_stack(ctx)
    removed = None
    for i in range(len(stack) - 1, -1, -1):
        if stack[i].get("run_id") == run_id:
            removed = stack.pop(i)
            break
    await ctx.set_state(_STACK_KEY, stack)
    return removed


async def active_run_id(ctx: Any) -> Optional[str]:
    """run_id du run actif (sommet de pile) ou None hors de tout run."""
    stack = await _read_stack(ctx)
    return stack[-1]["run_id"] if stack else None


# Marque de session : le run du jeton a déjà été cherché ici (trouvé ou non).
_CHERCHE_KEY = "oto_run_du_jeton_cherche"


def _run_ouvert_du_jeton(token_id: int) -> Optional[str]:
    """Le compte de l'appel, puis le run ouvert de son jeton — SYNC, joué hors de la
    boucle : l'identité elle-même peut lire la base (drain d'alias)."""
    from . import db
    from .auth.hooks import current_user_sub_from_token
    sub = current_user_sub_from_token()
    return db.run_ouvert_du_jeton(sub, token_id) if sub else None


async def reprendre_run_du_jeton(ctx: Any) -> Optional[str]:
    """Une session NEUVE d'un agent hébergé reprend le run que son jeton tient ouvert.

    La pile vit dans l'état de session : quand le relais d'un agent hébergé ouvre une
    autre session (relancé après un appel coupé côté client), la pile neuve est vide et
    chaque appel suivant partait au journal avec `run_id` NULL — la timeline du run
    s'arrêtait là, le travail continuait. Le relais retire `_run_id` des schémas servis
    au modèle : l'agent ne peut pas le repasser lui-même.

    Seul un jeton de DÉLÉGATION (un par travail) est suivi d'une session à l'autre, et
    seulement vers un run qu'il a OUVERT lui-même et qui n'est pas CLOS
    (`db.run_ouvert_du_jeton`) — jamais un jeton d'utilisateur, jamais un autre compte.
    Trouvé, le run est poussé sur la pile de CETTE session : la session reprend
    exactement l'état de la précédente, et `run_finish` l'y dépile. Cherché une fois par
    session (`_CHERCHE_KEY`), trouvé ou non. Rend le run repris, ou None."""
    from starlette.concurrency import run_in_threadpool

    from .auth.hooks import current_token_axes
    if ctx is None:
        return None
    axes = current_token_axes()
    if axes.get("token_kind") != "delegation" or axes.get("token_id") is None:
        return None
    try:
        if await ctx.get_state(_CHERCHE_KEY) or await _read_stack(ctx):
            return None
        run_id = await run_in_threadpool(_run_ouvert_du_jeton, int(axes["token_id"]))
        await ctx.set_state(_CHERCHE_KEY, True)
        if run_id is None:
            return None
        # Relue après l'attente : deux appels simultanés de la session ne l'empilent
        # pas deux fois (un doublon survivrait au `run_finish` qui en dépile un).
        stack = await _read_stack(ctx)
        if any(e.get("run_id") == run_id for e in stack):
            return run_id
        stack.append({"run_id": run_id, "label": None, "guide": None, "repris": True})
        await ctx.set_state(_STACK_KEY, stack)
        return run_id
    except Exception:  # noqa: BLE001 — la corrélation ne casse jamais l'appel
        logger.warning("reprise du run du jeton %s impossible (best-effort)",
                       axes.get("token_id"), exc_info=True)
        # Cherché quand même : une base qui refuse ne se relit pas à chaque appel.
        try:
            await ctx.set_state(_CHERCHE_KEY, True)
        except Exception:  # noqa: BLE001 — l'état de session est perdu, rien à marquer
            logger.warning("reprise du run du jeton : marque de session non posée",
                           exc_info=True)
        return None
