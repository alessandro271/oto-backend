"""Dépôt DIRECT d'un audio à transcrire (ADR 0074) — le fichier lui-même, pas sa référence.

- `POST /api/me/projects/{project_id}/transcriptions/upload` → multipart : `file` (l'audio),
  et en option `vocabulary` (mots séparés par virgules ou retours à la ligne) et
  `vocabulary_replace` (`true`|`false`). Rend `202 {job_id, status: "pending"}`, comme
  `POST …/transcriptions` qui prend une référence — même cœur (`capabilities.transcription.deposer`),
  même relecture (`GET /api/me/transcriptions/{job_id}`).

Corps binaire → hors couche capacité par nature (ADR 0009). L'audio n'est PAS rangé dans
les fichiers du projet : il ne sert qu'au travail, qui le purge après son tour. Le corps
se lit en flux, borné à `MAX_AUDIO_BYTES` (#562) — et seulement une fois le droit
d'écrire établi : un compte qui ne peut pas écrire ne fait rien téléverser.

La table de routes reste assemblée dans `api.routes.make_routes` ; ce module ne porte
que le handler.
"""
from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import db, file_source, ownership
from ..capabilities import transcription as _transcription
from ..capabilities._types import AuthzDenied
from .base import _authenticate, _json, _json_error
from .projects import _project_org_context_error
from .uploads import CorpsRefuse, lire_multipart_borne

_BOOLEENS = {"true": True, "false": False}


def _refus_projet(request: Request, sub: str, pid: int) -> JSONResponse | None:
    """Le projet existe, il est visible dans l'org de consultation (404 non-disclosant
    sinon, ADR 0023), et l'appelant peut y écrire."""
    if not db.get_project_by_id(pid):
        return _json_error(request, 404, "unknown_project")
    if (e := _project_org_context_error(request, sub, pid)):
        return e
    if not ownership.can_access(sub, "project", str(pid), "write"):
        return _json_error(request, 403, "forbidden")
    return None


async def transcription_upload(request: Request, *, verifier: JWTVerifier) -> JSONResponse:
    sub, err = await _authenticate(request, verifier)
    if err:
        return err
    pid = int(request.path_params["project_id"])
    if (refus := await run_in_threadpool(_refus_projet, request, sub, pid)):
        return refus
    try:
        form, upload, data = await lire_multipart_borne(request, _transcription.MAX_AUDIO_BYTES)
    except CorpsRefuse as e:
        return _json_error(request, e.status, e.code)
    remplace = _BOOLEENS.get(str(form.get("vocabulary_replace") or "false").strip().lower())
    if remplace is None:
        return _json_error(request, 400, "invalid_input",
                           "vocabulary_replace : `true` ou `false`.")
    fichier = file_source.ResolvedFile(
        data, getattr(upload, "filename", None) or "audio",
        getattr(upload, "content_type", None) or "application/octet-stream")
    try:
        ref = await run_in_threadpool(
            _transcription.deposer, sub, pid, lambda: fichier,
            vocabulary=(str(form.get("vocabulary") or "").strip() or None),
            vocabulary_replace=remplace)
    except AuthzDenied as d:
        return _json_error(request, d.status, d.code, d.message or None, d.details)
    return _json(request, ref, status=202)
