"""Les routes qu'un FOURNISSEUR appelle pour livrer un résultat qu'oto a commandé.

Une seule aujourd'hui : `POST /api/receivers/apollo/phones/{token}`, où Apollo livre
les téléphones d'un reveal (`apollo_receiver.py` décide, cette route adapte).

Écrite à la main, hors de la couche capacité, pour la raison du webhook des agents
(`api/hooks.py`) et de celui de Mollie (`api/billing.py`) : le corps est du JSON LIBRE,
que l'adaptateur de capacité refuserait champ par champ (`unknown_fields`), et
l'appelant n'a pas de JWT. **L'autorisation EST le jeton du chemin** — 256 bits,
propre à une commande, jamais stocké en clair, et masqué dans le journal comme tout
paramètre `{token}` (`journal_secrets.declare_routes`).

⚠️ Tout le travail base passe par `run_in_threadpool` (serveur mono-boucle).
⚠️ Aucun 5xx pour une raison métier : Apollo réessaie un 5xx. Un jeton inconnu est un
404 définitif ; seule une panne de NOTRE côté rend un 500, parce que là, réessayer
est la bonne conduite.
"""
from __future__ import annotations

import json
import logging

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import apollo_receiver

logger = logging.getLogger(__name__)


async def _lire_borne(request: Request, plafond: int) -> bytes | None:
    """Le corps, ou None dès qu'il dépasse le plafond — sans lire la suite."""
    declare = request.headers.get("content-length")
    if declare and declare.isdigit() and int(declare) > plafond:
        return None
    morceaux, total = [], 0
    async for morceau in request.stream():
        total += len(morceau)
        if total > plafond:
            return None
        morceaux.append(morceau)
    return b"".join(morceaux)


def _refus(statut: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": message}, status_code=statut)


async def apollo_phones(request: Request) -> JSONResponse:
    """Apollo livre les téléphones d'un reveal.

    Réponses :
      200 `{received: true}` — gardé ; `duplicate: true` si une livraison l'avait déjà
          été (retentative d'Apollo : rien n'est écrasé)
      400 le corps n'est pas du JSON
      404 jeton inconnu, abandonné ou expiré
      413 le corps dépasse le plafond
      500 panne de notre côté — réessayable
    """
    jeton = str(request.path_params.get("token", ""))
    if not apollo_receiver.forme_valide(jeton):
        return _refus(404, "receiver_not_found", "Unknown receiver.")
    brut = await _lire_borne(request, apollo_receiver.CORPS_MAX)
    if brut is None:
        logger.warning("receveur apollo : corps au-delà de %d octets refusé",
                       apollo_receiver.CORPS_MAX)
        return _refus(413, "payload_too_large",
                      f"Body above the {apollo_receiver.CORPS_MAX}-byte limit.")
    try:
        corps = json.loads(brut) if brut.strip() else None
    except ValueError:
        return _refus(400, "invalid_json", "The body is not JSON.")
    try:
        verdict = await run_in_threadpool(apollo_receiver.recevoir, jeton, corps)
    except Exception:  # noqa: BLE001 — journalisé, rendu réessayable
        logger.exception("receveur apollo : enregistrement impossible")
        return _refus(500, "receiver_failed", "Could not store the delivery. Retry.")
    if verdict == apollo_receiver.INCONNU:
        return _refus(404, "receiver_not_found", "Unknown receiver.")
    return JSONResponse({"received": True,
                         "duplicate": verdict == apollo_receiver.DOUBLON})


def make_routes(options_handler) -> list[Route]:
    return [
        Route(apollo_receiver.CHEMIN, apollo_phones, methods=["POST"]),
        Route(apollo_receiver.CHEMIN, options_handler, methods=["OPTIONS"]),
    ]
