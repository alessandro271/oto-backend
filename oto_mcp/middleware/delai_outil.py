"""`DelaiOutilMiddleware` — le DÉLAI d'un appel d'outil MCP, borne globale (#1232, lot 4).

Une borne du processus (`bornes_processus`), uniforme, `OTO_BORNE_OUTIL_S` (120 s) : la
même pour tous les outils — aucune borne propre à un outil (doctrine du 13/10/2026 : un
outil lourd ne se plafonne pas, on l'allège ; la mesure dit lequel).

- **Mesure** (`report`, défaut) : l'appel n'est pas touché ; s'il a duré plus que la
  borne, le dépassement va au palmarès des refus (limiteur `outil`, classe = le nom de
  l'outil, clé = empreinte de `user:<sub>` lu dans le jeton, sans base) et au journal.
- **Application** (`enforce`) : au-delà de la borne, l'attente est abandonnée et l'agent
  reçoit une erreur MCP `tool_timeout` qui dit le délai (`DelaiOutilDepasse`, classé
  par `error_taxonomy`). ⚠️ Un handler synchrone parti dans un fil y finit son travail :
  Python n'interrompt pas un fil. La borne rend la main à l'agent et la place dans la
  boucle ; elle ne défait pas une écriture en cours.

Sa place (`server._build_mcp`) : juste SOUS le journal d'appels (`ToolCallLogger`), qui
écrit donc la ligne de l'appel coupé avec son erreur, et sous l'enveloppe d'erreur, qui
la rend à l'agent ; au-dessus de Sentry, à qui une annulation n'apprend rien.
"""
from __future__ import annotations

import asyncio
import time

from fastmcp.server.middleware import Middleware

from .. import bornes_processus, limite_refus_top


def _cle_de_l_appelant() -> str:
    """L'empreinte de `user:<sub>` du jeton de l'appel — lue dans ses claims, sans base
    (`limite_refus_top.empreinte`, comme le porteur de la clé plateforme)."""
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        jeton = get_access_token()
    # noqa: SILENT — hors requête MCP authentifiée : l'appelant n'a pas de jeton
    except Exception:
        jeton = None
    sub = (getattr(jeton, "claims", None) or {}).get("sub")
    return limite_refus_top.empreinte(f"user:{sub}") if sub else "-"


class DelaiOutilMiddleware(Middleware):
    def __init__(self, reglages=None, horloge=time.monotonic) -> None:
        self._r = reglages
        self._horloge = horloge

    def _reglages(self):
        return self._r or bornes_processus.reglages()

    async def on_call_tool(self, context, call_next):
        r = self._reglages()
        borne = r.outil_s
        if not r.actif or borne <= 0:
            return await call_next(context)
        outil = getattr(context.message, "name", "") or "?"
        if r.applique:
            return await self._borne(context, call_next, outil, borne)
        debut = self._horloge()
        try:
            return await call_next(context)
        finally:
            duree = self._horloge() - debut
            if duree > borne:
                self._depasser(outil, duree, borne)

    async def _borne(self, context, call_next, outil: str, borne: int):
        """L'appel, annulé au-delà de `borne` secondes — dans la tâche COURANTE, pas une
        tâche à part (`asyncio.wait_for` en 3.10) : les variables de contexte que la
        chaîne pose pendant l'appel restent lisibles des middlewares au-dessus. Python
        3.10 (la box) n'a pas `asyncio.timeout` : c'est son mécanisme, écrit à la main."""
        tache = asyncio.current_task()
        coupe = False

        def _couper() -> None:
            nonlocal coupe
            coupe = True
            tache.cancel()

        minuterie = asyncio.get_running_loop().call_later(borne, _couper)
        try:
            return await call_next(context)
        except asyncio.CancelledError:
            if not coupe:
                raise                       # une annulation d'ailleurs (client parti…)
            if hasattr(tache, "uncancel"):  # 3.11+ : l'annulation est consommée ici
                tache.uncancel()
            self._depasser(outil, borne, borne)
            raise bornes_processus.DelaiOutilDepasse(borne) from None
        finally:
            minuterie.cancel()

    def _depasser(self, outil: str, duree: float, borne: int) -> None:
        bornes_processus.depasser(limite_refus_top.OUTIL, outil, _cle_de_l_appelant(),
                                  f"appel d'outil de {duree:.0f} s (borne {borne} s)",
                                  self._reglages().mode)
