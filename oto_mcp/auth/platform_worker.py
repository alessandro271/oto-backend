"""Le worker de plateforme de la REQUÊTE courante — posé par l'authentification
REST, lu par la règle d'autorisation. Rien d'autre ne l'écrit.

C'est un fait AUTHENTIFIÉ (le secret a été vérifié en base), pas une convention
sur la forme du `sub` : une règle qui déduirait « c'est un worker » d'un préfixe
de nom serait exactement le genre de déduction qu'on retire du worker lui-même.
"""
from __future__ import annotations

import contextvars
from typing import Optional

_CURRENT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "oto_platform_worker", default=None)


def set_current(row: Optional[dict]) -> None:
    _CURRENT.set(row)


def current() -> Optional[dict]:
    return _CURRENT.get()
