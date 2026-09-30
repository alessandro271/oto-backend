"""Le refus nommé d'une écriture qu'un connecteur ne câble pas : `<connecteur>_write_not_wired`.

Un connecteur en LECTURE SEULE garde ses outils d'écriture visibles, mais aucun n'appelle
l'amont : chacun rend ce refus, qui dit à l'agent ce que l'appel AURAIT fait. PayFit
(24/09/2026) puis Inqom (30/09/2026) — même forme, un seul domicile.

Une ERREUR, pas un résultat : un `{"sent": false}` se lit trop vite comme un succès, et
un agent qui croit avoir mis quelqu'un en paie ou passé une écriture comptable est pire
qu'un agent refusé. Même forme que les autres refus nommés (`connector_disabled`) : le
code dans le message ET dans `data`, `retryable: False`.

Le refus ne dépend de rien : ni la clé ni le client ne sont touchés. `what` décrit
l'action à l'agent — l'appelant n'y met que des identifiants, des dates, des libellés et
des totaux, jamais une valeur masquée par défaut (NIR, IBAN…), qui finirait dans le
journal des appels.
"""
from __future__ import annotations

from typing import Any

from mcp.types import ErrorData, INVALID_PARAMS

from ..mcp_errors import McpError


def refus(connecteur: str, libelle: str, op: str, action: str, **what: Any) -> McpError:
    """Le refus `<connecteur>_write_not_wired` pour `op` : « cela aurait <action> »,
    suivi des éléments fournis dans `what` (les vides sont omis)."""
    code = f"{connecteur}_write_not_wired"
    decrit = {k: v for k, v in what.items() if v is not None and v != "" and v != []}
    detail = ", ".join(f"{k}={v!r}" for k, v in decrit.items())
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Refus `{code}` : cela aurait {action}"
                 f"{f' ({detail})' if detail else ''} — mais le connecteur ne câble "
                 f"pas l'API {libelle} en écriture : rien n'a été envoyé à {libelle}. "
                 f"Ce connecteur ne fait que lire ; l'écriture se fait dans {libelle} "
                 "même."),
        data={"code": code, "retryable": False, "op": op, "would_have": decrit},
    ))
