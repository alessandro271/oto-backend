"""L'org d'un run, pour l'appel qui s'y fait (#639 — décision du 30/08/2026).

Sans axe `_org=`, un appel fait DANS un run se résolvait dans l'org MAISON du sub.
Mesuré en production le 29/08 (#631/#638) : un `data_write` sans `_org`, dans un run
ouvert sur l'org 226, résolu dans l'org 2 et refusé « namespace inconnu » sur un tableau
que la réservation du même run venait de résoudre — 82 refus sur sept jours, puis 109 sur
les sept suivants, tous des `data_write` du runner. Et le journal stampait la maison
(#630) : la vue filtrée par org ne montrait pas l'appel.

Décidé le 30/08 : **l'org d'un appel qui porte un run et aucun `_org` est
`runs.org_id`**. Trois règles :

- `_org=`/`_project=` explicites gardent la priorité — l'agent multi-org ne change pas
  (1 873 appels sur 32 115 en run sur sept jours, tous portés par un axe) ;
- l'appartenance reste exigée : un sub qui n'est pas (plus) membre de l'org du run est
  REFUSÉ, nommément — jamais un repli silencieux sur la maison (tourner sous une autre
  org que celle demandée est pire que se faire rejeter, ADR 0038) ;
- un run inconnu de `runs`, ou hors org, ne pose rien : `_run_id` y reste ce qu'il était,
  un identifiant opaque de corrélation (`call_axes._pin_run`).

**Le run appartient à son PROPRIÉTAIRE** (décision du 12/10/2026, revue de sécurité
F-014 — remplace « membre de l'org du run suffit ») : un `_run_id` est une poignée que
n'importe qui peut taper, et celui d'un collègue de la même org lui donnait les lignes
que ce run tient en réservation. Un run CONNU dont `runs.sub` n'est pas l'appelant est
refusé, `_org=` explicite ou non — même règle que la clôture (`db.finish_run`), la
libération (`run_finish`) et l'en-tête REST `X-Oto-Run` (`run_thread.run_de_l_en_tete`).
Le refus ne dit ni l'org ni le propriétaire du run.

Une lecture par appel, pas une par seam : le middleware résout ici, une fois, APRÈS les
axes, et pose une ContextVar que `access.current_org` relit entre le jeton et la maison.
`runs.sub` et `runs.org_id` sont immuables (seuls `outcome`/`note`/`finished_at` bougent
après la pose) — d'où un cache mémoire borné par run : le runner ouvre une session par
appel, et sans lui chaque appel d'un run relirait `runs`.
"""
from __future__ import annotations

import logging
from typing import Optional

from .mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS
from starlette.concurrency import run_in_threadpool

from . import call_axes, db, session_org

logger = logging.getLogger(__name__)

# run_id -> (sub, org_id). Insertion-ordered → éviction du plus ancien.
_TETE_DU_RUN: dict[str, tuple[Optional[str], Optional[int]]] = {}
_CAP = 10_000


def tete_du_run(run_id: str) -> Optional[tuple[Optional[str], Optional[int]]]:
    """`(runs.sub, runs.org_id)` du run — None si le run est inconnu. Un run connu est
    mis en cache (ni son propriétaire ni son org ne changent) ; un run inconnu ne l'est
    pas (sa ligne peut naître après — `run_start` la pose en best-effort)."""
    if run_id in _TETE_DU_RUN:
        return _TETE_DU_RUN[run_id]
    head = db.get_run_head(run_id)
    if head is None:
        return None
    org = head.get("org_id")
    tete = (head.get("sub"), int(org) if org is not None else None)
    _TETE_DU_RUN[run_id] = tete
    while len(_TETE_DU_RUN) > _CAP:
        del _TETE_DU_RUN[next(iter(_TETE_DU_RUN))]
    return tete


class RunRefuse(ValueError):
    """Le run n'est pas celui de l'appelant, ou son org n'est (plus) pas la sienne."""


def _refus(run_id: str, pourquoi: str) -> RunRefuse:
    # ⚠️ Ni le numéro ni le nom de l'org, ni le propriétaire : un `_run_id` est une
    # poignée que n'importe qui peut taper, et le refus lui aurait appris où se déroule
    # le run d'autrui et à qui il est. Le refus reste nommé et dit quoi faire.
    return RunRefuse(
        f"Le run `{run_id}` {pourquoi} : l'appel n'y est pas résolu, et il ne l'est pas "
        "non plus dans ton org maison à sa place. Ouvre ton propre run avec `run_start` "
        "(ou retire `_run_id`), et passe `_org=<une de tes orgs>` si besoin (liste : "
        "`oto_list_orgs`).")


def _resoudre(sub: str, run_id: str, *, poser: bool) -> Optional[int]:
    """Sync (threadpool) : garde le run pour l'appel, et rend son org à poser (None =
    rien à poser). Le propriétaire est exigé TOUJOURS ; l'org du run et l'appartenance
    — la même que lit l'autz (`roles.is_org_member`) — seulement si `poser` (aucun
    `_org=`/`_project=` explicite n'a déjà posé l'org)."""
    from . import roles
    tete = tete_du_run(run_id)
    if tete is None:
        return None
    proprietaire, org = tete
    if proprietaire != sub:
        raise _refus(run_id, "n'est pas le tien")
    if not poser or org is None:
        return None
    if not roles.is_org_member(sub, org):
        raise _refus(run_id, "se déroule dans une org dont tu n'es plus membre")
    return org


async def pin_for_call() -> list:
    """Garde le run de l'appel courant et pose son org, ou rien. À appeler APRÈS les
    axes : un `_org=`/`_project=` explicite a déjà posé l'org et garde la priorité —
    le run reste gardé (propriétaire), son org n'est alors ni posée ni jugée. Le sub
    se lit là où les gardes des axes le lisent (`call_axes`). Rend la liste
    d'annulation, même forme (LIFO) que les axes.

    ⚠️ Le run EXPLICITE seul (`current_call_run`), jamais le run de la session
    (`current_session_run`, #830) : ce dernier changerait l'org — ou ferait refuser,
    si l'on n'en est plus membre — des appels qui n'ont nommé aucun run."""
    run_id = session_org.current_call_run()
    if not run_id:
        return []
    # Sans jeton (endpoint anonyme) il n'y a personne à garder : la pose est inerte.
    # Un échec d'identité, lui, monte (journalisé par le seam, #464).
    sub = call_axes.current_user_sub_from_token()
    if not sub:
        return []
    poser = session_org.current_call_org() is None
    try:
        org = await run_in_threadpool(_resoudre, sub, run_id, poser=poser)
    except RunRefuse as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
    except Exception:
        logger.exception("run illisible pour sub=%s run=%s", sub, run_id)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Impossible de lire le run `{run_id}` (erreur interne) — "
                     "l'appel n'est pas résolu dans ton org maison à sa place. Réessaie, "
                     "ou passe `_org=` explicitement.")))
    if org is None:
        return []
    return [(session_org.reset_call_run_org, session_org.set_call_run_org(org))]
