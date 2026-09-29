"""`DynamicInstructionsMiddleware` — le contexte guide injecté dans la surface LLM."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware
from starlette.concurrency import run_in_threadpool

from ..auth.hooks import current_user_sub_from_token
from . import _handshake

logger = logging.getLogger(__name__)


_GUIDE_GET_TOOL = "oto_procedure"
_GUIDE_TOOL = "oto_guide"

# « Cette session n'est pas un endpoint de projet publié » — distinct de « projet
# publié SANS prose » (None), qui court-circuite quand même le socle plateforme.
_PAS_DE_PROJET_PUBLIE = object()


def _published_project_instructions():
    """Prose du projet publié servi à la session courante, ou `_PAS_DE_PROJET_PUBLIE`.

    **Sync (DB) — à appeler via `run_in_threadpool`** (serveur mono-loop). Lit une
    ContextVar (le projet anonyme du sous-domaine) : `run_in_threadpool` propage le
    contexte, même patron que `_reachable_suffix`."""
    from .. import instructions, subdomain_project
    pid = subdomain_project.current_anon_project_id()
    if not pid:
        return _PAS_DE_PROJET_PUBLIE
    return instructions.compose_published_project(pid)


def _index_par_outil(sub: str) -> dict:
    """Les index de prose on-demand à appender aux descriptions d'outils, par outil.

    **Sync (DB) — à appeler via `run_in_threadpool`** : résolution d'org, puis un index
    par outil, chacun cumulant plusieurs paliers (perso, org, équipe). Deux loaders,
    même canal de découverte : l'index per-(sub, org) enrichit la description de
    l'outil qui les charge."""
    from .. import access, guide_store, instructions
    org_id = access.current_org(sub)
    return {_GUIDE_GET_TOOL: instructions.skills_index_md(sub, org_id),
            _GUIDE_TOOL: guide_store.guides_index_md(sub, org_id)}


def _session_instructions(sub: str) -> str:
    """L'artefact A/C composé pour `sub` (org active incluse).

    **Sync (DB), et LOURD** : `compose_session` marche la cascade de statut de TOUS
    les connecteurs (`access.status_for`), soit plusieurs requêtes par connecteur —
    à appeler via `run_in_threadpool`, JAMAIS dans la boucle. Vécu en production dans
    la nuit du 15/08 : sous ~8 clients lourds, chaque `initialize` gelait l'event loop
    entier pendant la composition (py-spy : `on_initialize` → `compose_session` →
    `walk_cascade` → `psycopg execute` sur le MainThread, 3 relevés sur 6 dont ≥4 s
    consécutives) → 502 en rafale et « ASGI message after response already completed ».
    Cf. `docs/event-loop-perf.md` (mode de gel n°2)."""
    from .. import access, instructions
    return instructions.compose_session(sub, access.current_org(sub))


class DynamicInstructionsMiddleware(Middleware):
    """Injecte le contexte guide de l'org dans la surface vue par le LLM, par-(sub,
    org), au lieu de dépendre d'un appel volontaire de lecture de guide (canal fragile,
    otomata-private#49, amende ADR 0014). Deux points d'injection, selon la NATURE :

    - **artefact composé** (blocs A/C, #50) → `on_initialize` REMPLACE les
      instructions de la session par `instructions.compose_session(sub, org)`, posées
      AVANT la réponse (`_handshake.poser_avant_reponse`) — après, elles ne partent pas
      (le « cheval de Troie », relu par session ; Claude rehandshake par conversation).
    - **index des guides NOMMÉS** (skills) → `on_list_tools` enrichit la
      **description de `oto_procedure`** (l'outil qui les charge). Les skills ne sont
      PAS des outils → absents de `tools/list` → ce serait leur seul canal. Co-localisé
      avec le loader plutôt qu'un bloc dans les instructions.

    Fail-open partout : pas de sub (stdio/discovery), pas d'org, ou erreur → surface
    statique inchangée.
    """

    async def on_initialize(self, context, call_next):
        # ⚠️ Tout se résout AVANT `call_next` : la réponse au `initialize` part PENDANT
        # `call_next` (fastmcp), construite depuis les options de la session. Modifier
        # `result` ensuite ne change rien à ce que le client reçoit — le défaut qui
        # laissait chaque client sur la surface statique (`_handshake`,
        # `tests/middleware/test_handshake_livre.py`).
        #
        # Endpoint de PROJET publié : le client est un tiers sans compte. Il reçoit la
        # prose du projet, jamais le socle plateforme (feedback #309) — cf.
        # `instructions.compose_published_project`.
        #
        # ⚠️ Les DEUX compositions ci-dessous sont du DB SYNC → `run_in_threadpool`
        # obligatoire : ce hook s'exécute DANS la boucle (un middleware fastmcp est
        # async par contrat), et le serveur est mono-loop. Gel de prod du 15/08.
        corps = None
        projet_publie = False
        try:
            body = await run_in_threadpool(_published_project_instructions)
        except Exception:
            logger.warning("instructions de projet publié échouées (fail-open)",
                           exc_info=True)
        else:
            if body is not _PAS_DE_PROJET_PUBLIE:
                projet_publie, corps = True, body or None
        if not projet_publie:
            try:
                sub = current_user_sub_from_token()
            # noqa: SILENT — dette déclarée : sub avalé, la requête devient anonyme sans dire pourquoi (#424, verdict C)
            except Exception:
                sub = None
            if sub:
                try:
                    corps = await run_in_threadpool(_session_instructions, sub)
                except Exception:
                    logger.warning("composition des instructions échouée pour sub=%s "
                                   "(fail-open)", sub, exc_info=True)
        if corps:
            _handshake.poser_avant_reponse(context, instructions=corps)
        result = await call_next(context)
        # Aligné sur ce qui est parti, pour ce qui relit `result` côté serveur. Un
        # serveur sans instructions statiques n'en reçoit pas (règle d'avant).
        if corps and result is not None and getattr(result, "instructions", None):
            result.instructions = corps
        return result

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        try:
            sub = current_user_sub_from_token()
        # noqa: SILENT — dette déclarée : sub avalé, la requête devient anonyme sans dire pourquoi (#424, verdict C)
        except Exception:
            sub = None
        if not sub:
            return tools
        try:
            # ⚠️ HORS BOUCLE (09/09/2026). Ces trois seams sont SYNC et lisent la base :
            # résolution d'org, puis les deux index (qui interrogent chacun plusieurs
            # paliers). Ils tournaient dans le thread de l'event loop — le mode de panne
            # exact de `docs/event-loop-perf.md`, resté ici parce que ce hook n'était pas
            # couvert par la garde. Le cumul des paliers personnel et équipe y ajoute des
            # requêtes : le déplacer AVANT d'en ajouter, pas après.
            extra = await run_in_threadpool(_index_par_outil, sub)
            if not any(extra.values()):
                return tools
            return [
                t.model_copy(update={"description":
                                     f"{(t.description or '').rstrip()}\n\n{extra[t.name]}"})
                if extra.get(t.name) else t
                for t in tools
            ]
        except Exception:
            logger.warning("enrichissement d'index (procédures/guides) échoué pour sub=%s "
                           "(fail-open)", sub, exc_info=True)
            return tools
