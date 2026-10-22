"""La clé `anthropic` vérifiée CHEZ ANTHROPIC à la pose — un appel léger et sans coût.

La forme de la clé (`CredentialField.prefixes`) ne dit pas si elle MARCHE. Deux défauts
passaient la pose et n'éclataient qu'au premier travail, chez le worker :
- une clé révoquée, expirée ou d'un autre compte ;
- une clé d'ORGANISATION (créée hors d'un workspace) déposée sans son workspace :
  Anthropic refuse alors TOUTE requête qui ne porte pas l'en-tête
  `anthropic-workspace-id`.

Le test : `GET /v1/models?limit=1` — une lecture, aucun jeton consommé, avec la clé
(`x-api-key`), la version d'API, et le workspace s'il est saisi. Forme d'erreur attestée
par la documentation d'Anthropic (Authentication, « Select a workspace »,
https://platform.claude.com/docs/en/manage-claude/authentication#select-a-workspace) :
une clé non rattachée à un workspace, sans l'en-tête, reçoit un HTTP 400

    {"type": "error",
     "error": {"type": "invalid_request_error",
               "message": "This API key is not scoped to a workspace, so this request
                           must include the anthropic-workspace-id header with the ID of
                           the workspace to use. Add the header, or use an API key that
                           is scoped to a workspace."},
     "request_id": "req_…"}

et, avec l'en-tête, un identifiant mal formé rend 400 (`anthropic-workspace-id header must
be a valid workspace ID.`), un workspace inconnu ou inaccessible 404 (`not_found_error`).

**Aucun repli** : Anthropic injoignable (réseau, délai, 5xx, 429) REFUSE la pose. Une clé
non vérifiée ne se dépose pas — c'est l'objet de ce module.

⚠️ Ni la clé ni le workspace n'entrent dans un message, une exception ou un journal : le
texte d'Anthropic relayé en est expurgé, une erreur réseau ne rend que son TYPE (l'objet
`httpx` porte la requête, donc ses en-têtes), et rien n'est chaîné.

⚠️ Client SYNCHRONE, et c'est voulu : la pose partagée (`credentials_store.preparer_pose`)
est appelée par des handlers de capacité SYNCHRONES, que les adaptateurs jouent dans le
threadpool (`capabilities/_execution.py`) — jamais dans la boucle. Délai borné à
`DELAI_S`.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .credentials_store import CredentialFieldsInvalid

logger = logging.getLogger(__name__)

URL = "https://api.anthropic.com/v1/models"
VERSION = "2023-06-01"
DELAI_S = 10.0
#: Ce que la doc d'Anthropic met dans le message du 400 d'une clé d'organisation sans
#: en-tête : le NOM de l'en-tête. Un 400 sans workspace saisi qui ne le nomme pas est
#: relayé tel quel, jamais lu comme ce cas-là.
_ENTETE = "anthropic-workspace-id"

CLE_D_ORGANISATION = ("clé d'organisation Anthropic : renseigner le champ Workspace, "
                      "ou déposer une clé créée dans un workspace")

WORKSPACE_REQUIS = "anthropic_workspace_required"
CLE_REFUSEE = "anthropic_key_rejected"
INJOIGNABLE = "anthropic_unreachable"

_PAS_DEPOSEE = "la clé n'est pas déposée"


def _client() -> httpx.Client:
    """Le client de l'appel — seam des bancs, qui y montent un transport bouchon."""
    return httpx.Client(timeout=DELAI_S)


def _texte_d_anthropic(r: httpx.Response, *secrets: Optional[str]) -> str:
    """Le `error.message` d'Anthropic, borné et EXPURGÉ de ce qu'on lui a envoyé."""
    try:
        corps = r.json()
    except ValueError:
        corps = None
    erreur = corps.get("error") if isinstance(corps, dict) else None
    texte = str(erreur.get("message") or "") if isinstance(erreur, dict) else ""
    for s in secrets:
        if s:
            texte = texte.replace(s, "…")
    return texte.strip()[:300]


def verifier(cle: str, workspace_id: Optional[str] = None) -> None:
    """Rend `None` si Anthropic accepte la clé (avec son workspace s'il est saisi) ;
    sinon lève `CredentialFieldsInvalid` — code pour la machine, phrase pour l'humain."""
    workspace_id = (workspace_id or "").strip() or None
    entetes = {"x-api-key": cle, "anthropic-version": VERSION}
    if workspace_id:
        entetes[_ENTETE] = workspace_id
    try:
        with _client() as client:
            r = client.get(URL, params={"limit": 1}, headers=entetes)
    except httpx.HTTPError as e:
        logger.warning("pose d'une clé anthropic : Anthropic injoignable (%s)",
                       type(e).__name__)
        raise CredentialFieldsInvalid(
            INJOIGNABLE,
            f"Anthropic injoignable ({type(e).__name__}) : la clé n'a pas pu être "
            f"vérifiée, {_PAS_DEPOSEE} — réessayer dans un instant.") from None

    code = r.status_code
    if code == 200:
        return
    detail = _texte_d_anthropic(r, cle, workspace_id)
    suite = f" : {detail}" if detail else ""
    if code == 400 and not workspace_id and _ENTETE in detail:
        raise CredentialFieldsInvalid(WORKSPACE_REQUIS, CLE_D_ORGANISATION)
    if code == 401:
        raise CredentialFieldsInvalid(
            CLE_REFUSEE, f"clé Anthropic refusée (HTTP 401) : clé invalide, révoquée ou "
                         f"expirée — {_PAS_DEPOSEE}.")
    if code == 403:
        raise CredentialFieldsInvalid(
            CLE_REFUSEE, f"clé Anthropic sans droit (HTTP 403){suite} — {_PAS_DEPOSEE}.")
    if code == 404 and workspace_id:
        raise CredentialFieldsInvalid(
            CLE_REFUSEE, "Workspace introuvable, ou inaccessible à cette clé (HTTP 404) — "
                         f"vérifier l'identifiant `wrkspc_…` du champ Workspace ; "
                         f"{_PAS_DEPOSEE}.")
    if code == 429 or code >= 500:
        logger.warning("pose d'une clé anthropic : Anthropic a répondu HTTP %s", code)
        raise CredentialFieldsInvalid(
            INJOIGNABLE, f"Anthropic n'a pas pu vérifier la clé (HTTP {code}){suite} — "
                         f"{_PAS_DEPOSEE}, réessayer dans un instant.")
    raise CredentialFieldsInvalid(
        CLE_REFUSEE, f"Anthropic refuse la clé (HTTP {code}){suite} — {_PAS_DEPOSEE}.")
