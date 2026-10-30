"""Which model answers a conversation (`docs/copilot.md` §Quel modèle répond).

A conversation starts on the instance's DEFAULT model (`OTO_COPILOT_MODEL`, Claude Sonnet
5.5) and moves to its STRONGER one (`OTO_COPILOT_MODEL_UP`, Claude Opus 5.5) on a fixed
list of signs that the work is hard — never by asking a model to judge itself:

- it changes what makes other things run or behave later — an agent, a schedule, a fleet,
  a recipe, a function, a process, a guide, a profile, the org's settings
  (`classement.lourd`) — or so much as calls a tool that reads an agent, a schedule, a
  fleet, a recipe or a function (`classement.automatisation`; a tool search that only
  loads one does not count);
- the model refused the request (`refusal`);
- two tool batches in a row failed;
- the person declined a card.

**One way, and sticky.** Once up, the conversation stays up; a new conversation starts on
the default again. Never down inside one: a switch restarts the provider's cache, and the
stronger model reads the default one's thinking blocks, not the reverse.

**A conversation created before this existed has no model** (`model` NULL): it keeps the
model and the tool list it was started with — changing either under it would invalidate
its cache and, on these models, its earlier reasoning.
"""
from __future__ import annotations

import logging
import os

from . import classement
from .modele import MODELE

logger = logging.getLogger(__name__)

DEFAUT = "claude-sonnet-5-5"
FORT = "claude-opus-5-5"
#: What a conversation without a model ran on: the model of the first version.
HERITE = MODELE
#: Consecutive failed tool batches before the conversation moves up.
ECHECS_DE_SUITE = 2


class ModeleInconnu(ValueError):
    """`OTO_COPILOT_MODEL` or `OTO_COPILOT_MODEL_UP` names a model with no provider price
    (`depense.PRIX_PAR_MTOK`)."""


def _lu(env: str, defaut: str) -> str:
    """The model the instance names, or the default when unset. Set to a model with no
    known price (empty included): `ModeleInconnu` — never the default in silence, a typo
    would run every conversation on a model nobody chose. Checked at startup
    (`verifier_reglages`) and on each read."""
    from .depense import PRIX_PAR_MTOK
    brut = os.environ.get(env)
    if brut is None:
        return defaut
    nom = brut.strip()
    if nom not in PRIX_PAR_MTOK:
        raise ModeleInconnu(f"{env}={brut!r}: no provider price for this model (one of "
                            f"{', '.join(sorted(PRIX_PAR_MTOK))})")
    return nom


def defaut() -> str:
    return _lu("OTO_COPILOT_MODEL", DEFAUT)


def fort() -> str:
    return _lu("OTO_COPILOT_MODEL_UP", FORT)


def verifier_reglages() -> None:
    """The startup guard: an unknown model refuses to start, rather than failing the
    first conversation (or its first move up) in front of a person."""
    defaut()
    fort()


def modele_du_fil(fil: dict) -> str:
    return fil.get("model") or HERITE


def outils_a_la_demande(fil: dict) -> bool:
    """A conversation started with a model loads most of its tools on demand."""
    return bool(fil.get("model"))


def monter(fil: dict, raison: str) -> bool:
    """Moves the conversation to the stronger model. True = it moved (it was not up yet,
    and is not a conversation of the first version)."""
    if not fil.get("model"):
        return False
    haut = fort()
    if fil["model"] == haut:
        return False
    logger.info("copilot : conversation %s passe de %s à %s (%s)",
                fil.get("id"), fil["model"], haut, raison)
    fil["model"] = haut
    return True


def _cibles(message: dict) -> list[tuple[str, dict]]:
    """The tools the message calls, `oto_call` unwrapped to its target."""
    return [classement.cible(b.get("name") or "", b.get("input") or {})
            for b in message.get("content") or [] if b.get("type") == "tool_use"]


def ecrit_un_lourd(message: dict, annotations) -> bool:
    """The message calls a hard target to change it."""
    return any(classement.lourd(t) and
               classement.classer(t, a, annotations(t)) != classement.LECTURE
               for t, a in _cibles(message))


def touche_une_automatisation(message: dict) -> bool:
    """The message CALLS an agent, a schedule, a fleet, a recipe or a function tool, to
    read it or not. A tool search that merely LOADS one does not count: the search
    returns several tools by text match, and a side hit is no sign of hard work."""
    return any(classement.automatisation(t) for t, _ in _cibles(message))


def a_parle(message: dict) -> bool:
    """The person already saw words of this message (streamed as they came)."""
    return any(b.get("type") == "text" and (b.get("text") or "").strip()
               for b in message.get("content") or [])
