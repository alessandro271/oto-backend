"""Poser ce que le client reçoit au `initialize` — AVANT que la réponse ne parte.

⚠️ Dans fastmcp (`fastmcp/server/low_level.py`, `call_original_handler`), la session MCP
RÉPOND au `initialize` pendant `call_next`, et ne rend la main au middleware
qu'ensuite. La réponse est construite depuis `session._init_options` (instructions,
`serverInfo.name`). Un middleware qui modifie `result` APRÈS `call_next` ne change
donc rien à ce que le client a reçu — c'est ce qui laissait tous les clients sur la
surface statique du démarrage (`tests/middleware/test_handshake_livre.py`).

La seule voie : poser les champs sur les options de CETTE session avant `call_next`.
Une copie par session — jamais l'objet partagé du serveur, sinon un compte verrait le
socle d'un autre.

`_init_options` est un champ PRIVÉ de la session MCP. Ce module est le seul endroit qui
le touche ; `test_le_seam_prive_de_fastmcp_est_toujours_la` rougit si une montée de
version le déplace (le pin `fastmcp<3.5` borne le risque, il ne l'annule pas).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def poser_avant_reponse(context, **champs) -> bool:
    """Remplace, pour la session de ce `initialize`, les champs d'`InitializationOptions`
    donnés (`instructions`, `server_name`…). À appeler AVANT `call_next`.

    Ne REMPLACE qu'un champ que le serveur sert déjà : un serveur démarré sans
    instructions n'en reçoit pas d'un middleware (même règle qu'avant ce module).

    Rend True si quelque chose est posé. Fail-open : pas de session (tests unitaires,
    transport sans session) ou champ absent ⟹ False et rien de changé — le handshake
    passe avec la surface statique, comme avant."""
    ctx = getattr(context, "fastmcp_context", None)
    session = getattr(ctx, "session", None) if ctx is not None else None
    options = getattr(session, "_init_options", None) if session is not None else None
    if options is None:
        if ctx is not None:
            logger.warning("handshake : pas d'options de session à poser (%s) — la "
                           "surface statique part telle quelle", sorted(champs))
        return False
    maj = {k: v for k, v in champs.items() if v and getattr(options, k, None)}
    if not maj:
        return False
    session._init_options = options.model_copy(update=maj)
    return True
