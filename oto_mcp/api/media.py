"""Handlers des IMAGES de compte et d'organisation — avatar user, logo d'org.

- `POST|DELETE /api/me/avatar`      → avatar de l'utilisateur courant
- `POST|DELETE /api/orgs/{id}/logo` → logo UPLOADÉ de l'org (org_admin)

Upload multipart → ne passe PAS par la couche capacité (ADR 0009 = corps JSON
pydantic), d'où des routes écrites à la main. L'URL publique est persistée en clair
(ce n'est pas un secret). Le logo AFFICHÉ reste l'EFFECTIF (upload sinon dérivé
logo.dev du domaine déclaré) : `org_store.effective_logo_url`.

La table de routes (chemins, méthodes, ORDRE) reste assemblée dans
`api.routes.make_routes` ; ce module ne porte que les handlers.
"""
from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import db, media_store, org_store
from ..capabilities._types import ContratDeRoute, DeclaredError
from .base import _authenticate, _json, _json_error


async def _read_upload(request: Request):
    """Parse un multipart, renvoie (data: bytes, err: JSONResponse|None)."""
    try:
        form = await request.form()
    except Exception:
        return None, _json_error(request, 400, "invalid_multipart")
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return None, _json_error(request, 400, "missing_file")
    return await upload.read(), None


async def avatar_save(request: Request, *, verifier: JWTVerifier) -> JSONResponse:
    sub, err = await _authenticate(request, verifier)
    if err:
        return err
    data, err = await _read_upload(request)
    if err:
        return err
    try:
        url = media_store.upload_image("avatars", sub, data, "")
    except media_store.MediaError as e:
        return _json_error(request, e.status, e.code)
    old = (db.get_user(sub) or {}).get("avatar_url")
    db.set_avatar_url(sub, url)
    if old and old != url:
        media_store.delete_by_url(old)
    return _json(request, {"ok": True, "avatar_url": url})


def _org_logo_gate(request: Request, sub: str):
    """Renvoie (org_id, err). 400 id invalide, 404 org inconnue, 403 non-admin."""
    from .. import roles
    try:
        org_id = int(request.path_params["id"])
    except (ValueError, KeyError):
        return None, _json_error(request, 400, "invalid_id")
    if not org_store.get_org(org_id):
        return None, _json_error(request, 404, "unknown_org")
    if not roles.is_org_admin(sub, org_id):
        return None, _json_error(request, 403, "forbidden")
    return org_id, None


async def org_logo_save(request: Request, *, verifier: JWTVerifier) -> JSONResponse:
    sub, err = await _authenticate(request, verifier)
    if err:
        return err
    org_id, err = _org_logo_gate(request, sub)
    if err:
        return err
    data, err = await _read_upload(request)
    if err:
        return err
    try:
        url = media_store.upload_image("org-logos", str(org_id), data, "")
    except media_store.MediaError as e:
        return _json_error(request, e.status, e.code)
    old = (org_store.get_org(org_id) or {}).get("logo_url")
    org_store.set_org_logo(org_id, url)
    if old and old != url:
        media_store.delete_by_url(old)
    return _json(request, {"ok": True, "logo_url": url})


# ── Le contrat publié (#655) ─────────────────────────────────────────────────────
# Routes de NATURE (corps multipart, hors du moule capacité) : elles portent leur
# contrat comme la réception d'un upload signé (`ContratDeRoute`, oto#106). Les refus
# déclarés sont ceux que les handlers ci-dessus rendent — la garde
# `tests/test_capability_declared_errors.py` le vérifie sur le chemin réel.

class AvatarSaved(BaseModel):
    ok: bool
    avatar_url: str


class LogoSaved(BaseModel):
    ok: bool
    logo_url: str


_IMAGE = {
    "type": "object", "required": ["file"],
    "properties": {"file": {
        "type": "string", "format": "binary",
        "description": ("png, jpeg, gif ou webp — jugé sur les OCTETS, jamais sur le "
                        "`Content-Type` déclaré ; 2 Mo au plus par défaut (borne "
                        "réglable par instance, refus `image_too_large` au-delà)")}},
}

_REFUS_IMAGE = (
    DeclaredError(400, "invalid_multipart", "le corps n'est pas un multipart lisible"),
    DeclaredError(400, "missing_file", "aucune partie `file`, ou un fichier vide"),
    DeclaredError(400, "unsupported_type",
                  "ni png, ni jpeg, ni gif, ni webp (jugé sur les octets)"),
    DeclaredError(413, "image_too_large", "au-delà de la borne d'image (2 Mo par défaut)"),
)

avatar_save.contrat = ContratDeRoute(
    description=(
        "Pose mon avatar. Corps multipart, une seule partie `file` : l'image (png, jpeg, "
        "gif ou webp, reconnue à ses octets). L'ancienne image stockée est supprimée. "
        "Rend l'adresse publique de la nouvelle. Pour l'effacer : `DELETE` sur le même "
        "chemin."),
    Output=AvatarSaved,
    errors=_REFUS_IMAGE,
    corps={"POST": {"multipart/form-data": _IMAGE}},
)

org_logo_save.contrat = ContratDeRoute(
    description=(
        "Pose le logo UPLOADÉ de l'org (org_admin). Corps multipart, une seule partie "
        "`file` : l'image (png, jpeg, gif ou webp, reconnue à ses octets). Le logo "
        "AFFICHÉ est l'upload s'il existe, sinon celui dérivé du domaine de marque "
        "déclaré. Pour retirer l'upload : `DELETE` sur le même chemin."),
    Output=LogoSaved,
    errors=(
        DeclaredError(400, "invalid_id", "l'identifiant d'org du chemin n'est pas un entier"),
        DeclaredError(404, "unknown_org", "aucune org sous cet identifiant"),
        DeclaredError(403, "forbidden", "l'appelant n'est pas org_admin de cette org"),
        *_REFUS_IMAGE,
    ),
    corps={"POST": {"multipart/form-data": _IMAGE}},
)
