"""Ce qui RESTE de forme JSON dans les images de compte et les fichiers de projet.

Cinq routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `DELETE /api/me/avatar`                                  → efface mon avatar
- `POST   /api/me/avatar/import`  (ajoutée le 20/11/2026)  → ma photo de profil Google
- `DELETE /api/orgs/{id}/logo`                             → efface le logo UPLOADÉ de l'org
- `GET    /api/me/projects/{project_id}/files`             → liste les fichiers bruts
- `DELETE /api/me/projects/{project_id}/files/{file_id}`   → en supprime un
- `POST   /api/me/projects/{project_id}/files/{file_id}/public` → bascule son partage

⚠️ **Leurs quatre voisines ne migrent pas, et sont reclassées `NATURE`** dans
`tests/test_rest_modules_are_capabilities.py` : les trois `POST` multipart (avatar, logo
d'org, dépôt de fichier) et l'export ZIP d'un projet. L'adaptateur REST lit un corps
JSON et répond en JSON ; un corps binaire et une réponse `application/zip` sont hors du
moule par CONSTRUCTION, exactement comme `/api/upload/{token}`. On ne déforme pas
l'adaptateur pour quatre routes — on dit pourquoi elles restent dehors.

⚠️ **Duplication assumée avec `me.project_files`** (MCP `oto_project_files`), qui fait
déjà `list` et `delete` pour l'agent. Les RÉPONSES sont les mêmes ; ce qui diffère, ce
sont les REFUS (la face MCP joint un message à ses 404, la face REST rend le code nu) et
la forme d'entrée (`op` + `project_id` contre des paramètres de chemin). Fusionner les
deux changerait donc les corps servis au dashboard — hors du « mêmes réponses au
caractère près » de ce chantier. C'est la même famille de décision que la toolbox
(oto-backend#429), et elle est suivie à part.

**Pas de face MCP** ici : la lecture et la suppression sont déjà couvertes par
`oto_project_files`, et `oto_resource` porte le partage d'une ressource possédée. Ces
cinq-là sont les chemins du dashboard.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel

from .. import access, db, org_store
from . import _publication
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_FILES = "/api/me/projects/{project_id:int}/files"
_FILE = _FILES + "/{file_id:int}"


# --- Entrées ----------------------------------------------------------------

class AvatarClearInput(BaseModel):
    """Aucun paramètre : l'avatar effacé est celui du porteur du jeton."""


class AvatarImportInput(BaseModel):
    """L'adresse `picture` du jeton d'IDENTITÉ émis à la connexion (la photo du compte
    Google). Texte libre ici : l'hôte est jugé dans le handler, qui rend son propre
    refus nommé (`url_not_allowed`) plutôt que l'`invalid_input` de pydantic."""
    url: str


class OrgLogoClearInput(BaseModel):
    # Texte, pas entier : la route rend `400 invalid_id`, pas le `invalid_input` de
    # pydantic. On convertit dans le handler pour garder le code servi.
    org_id: str = ""


class ProjectFilesListInput(BaseModel):
    project_id: int


class ProjectFileInput(BaseModel):
    project_id: int
    file_id: int


class ProjectFilePublicInput(BaseModel):
    project_id: int
    file_id: int
    # ⚠️ **REQUIS, et c'est un durcissement VOULU.** Le handler d'origine faisait
    # `bool(isinstance(body, dict) and body.get("public"))` : un corps sans `public`
    # valait donc « rendre privé », en silence. Combiné à l'adaptateur — qui avale un
    # corps illisible et le traite comme absent —, un JSON malformé aurait DÉPARTAGÉ le
    # fichier en rendant 200, là où la route rendait un 400 franc. On refuse plutôt que
    # d'agir : c'est la règle du dépôt, et ici l'action silencieuse était destructrice.
    public: bool


# --- Sorties ----------------------------------------------------------------

class Cleared(BaseModel):
    ok: bool


class AvatarImported(BaseModel):
    ok: bool
    # Faux = rien n'a changé : la personne avait déjà un avatar (un upload gagne).
    imported: bool
    # L'avatar SERVI après l'appel : l'importé, ou celui qui était déjà là.
    avatar_url: Optional[str] = None


class ProjectFile(BaseModel):
    """⚠️ `s3_key` n'en fait PAS partie : la clé de stockage est retirée de chaque ligne
    et remplacée par `download_url`, une URL **signée et temporaire**. Elle expire — on
    ne la met pas en cache, on redemande la liste.

    `public_url` est l'autre régime : permanente, servie tant que le partage est ouvert.
    `null` = fichier privé."""
    id: int
    project_id: int
    filename: Optional[str] = None
    mime: Optional[str] = None
    size_bytes: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    # Résumé produit à l'ingestion, quand le format s'y prête.
    summary: Optional[str] = None
    public: Optional[bool] = None
    public_url: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[Any] = None
    # `null` quand le stockage n'a pas pu signer : la ligne reste servie, sans lien.
    download_url: Optional[str] = None


class ProjectFileList(BaseModel):
    files: list[ProjectFile]


class ProjectFileSaved(BaseModel):
    ok: bool
    file: ProjectFile


# --- Gardes partagées -------------------------------------------------------

def _org_context(ctx: ResolvedCtx, pid: int) -> None:
    """Gate de CONTEXTE d'org (ADR 0023) des routes projet par-id : le projet doit être
    visible dans l'org de CONSULTATION, pas seulement accessible à l'acteur via une
    AUTRE de ses orgs (fuite cross-org). 404 non-disclosante."""
    from .. import ownership
    if not ownership.visible_in_org(ctx.sub, access.current_org(ctx.sub), "project", str(pid)):
        raise AuthzDenied(404, "unknown_project")


def _signed(row: dict) -> dict:
    from .. import media_store
    key = row.pop("s3_key", None)
    try:
        row["download_url"] = media_store.presign_get(key) if key else None
    except media_store.MediaError:
        row["download_url"] = None
    return row


def _fichier(ctx: ResolvedCtx, pid: int, file_id: int) -> dict:
    """La ligne du fichier, après les trois gates d'écriture, dans l'ORDRE servi :
    existence + appartenance au projet (404), contexte d'org (404), permission (403)."""
    from .. import ownership
    existing = db.get_project_file(file_id)
    if not existing or existing["project_id"] != pid:
        raise AuthzDenied(404, "unknown_file")
    _org_context(ctx, pid)
    if not ownership.can_access(ctx.sub, "project", str(pid), "write"):
        raise AuthzDenied(403, "forbidden")
    return existing


# --- Handlers ---------------------------------------------------------------

def _avatar_clear(ctx: ResolvedCtx, inp: AvatarClearInput) -> dict:
    old = (db.get_user(ctx.sub) or {}).get("avatar_url")
    db.set_avatar_url(ctx.sub, None)
    if old:
        from .. import media_store
        media_store.delete_by_url(old)
    return {"ok": True}


# L'import de la photo de profil (20/11/2026). Le jeton d'IDENTITÉ porte `picture`
# (la photo du compte Google) ; le jeton d'ACCÈS que lit ce service ne la porte pas, et le
# navigateur du tableau de bord ne peut pas télécharger l'image lui-même. Le tableau de
# bord envoie donc l'adresse, une fois, et le serveur la récupère, la RE-HÉBERGE (une
# adresse Google peut changer ou se fermer) et la pose — seulement sur un compte SANS
# avatar : un upload gagne toujours. L'adresse vient du client : elle ne vise QUE les
# hôtes d'images de Google, n'en sort jamais par une redirection, et passe la garde
# d'egress sous la politique « URL choisie » (ce qui résout vers l'intérieur est refusé,
# sans exception possible).

_HOTES_PHOTO = ("lh3.googleusercontent.com",)
_SUFFIXE_PHOTO = ".googleusercontent.com"
_DELAI_PHOTO = (3, 5)
_REDIRECTIONS_PHOTO = 2


def _hote_de_photo(url: str) -> bool:
    morceaux = urlsplit(url or "")
    hote = (morceaux.hostname or "").lower()
    return (morceaux.scheme == "https" and not morceaux.username and not morceaux.password
            and (hote in _HOTES_PHOTO or hote.endswith(_SUFFIXE_PHOTO)))


def _adresse_de_photo(url: str) -> None:
    """Refuse toute adresse qui n'est pas une image de Google publique."""
    from .. import egress
    if not _hote_de_photo(url):
        raise AuthzDenied(400, "url_not_allowed")
    try:
        egress.check_url(url, connector="avatar_import", field="url",
                         exceptions_declarees=False)
    except egress.EgressRefused:
        raise AuthzDenied(400, "url_not_allowed") from None


def _telecharger_photo(url: str) -> bytes:
    """Les octets de la photo. Lecture en flux, bornée PENDANT (la borne d'image du
    stockage), aucune redirection suivie hors des hôtes de Google."""
    import requests

    from .. import egress, egress_transport, media_store
    borne = media_store.max_image_bytes()
    courante = url
    # Session épinglée : la garde tient à la connexion, sur l'adresse jointe.
    with egress_transport.session(connector="avatar_import", field="url",
                                  exceptions_declarees=False) as s:
        for _ in range(_REDIRECTIONS_PHOTO + 1):
            _adresse_de_photo(courante)
            try:
                r = s.get(courante, stream=True, allow_redirects=False,
                          timeout=_DELAI_PHOTO)
            except egress.EgressRefused:
                raise AuthzDenied(400, "url_not_allowed") from None
            except requests.RequestException:
                raise AuthzDenied(502, "fetch_failed") from None
            try:
                if r.is_redirect or r.is_permanent_redirect:
                    courante = urljoin(courante, r.headers.get("Location") or "")
                    continue
                if r.status_code != 200:
                    raise AuthzDenied(502, "fetch_failed")
                if not (r.headers.get("Content-Type") or "").lower().startswith("image/"):
                    raise AuthzDenied(400, "unsupported_type")
                morceaux, total = [], 0
                for morceau in r.iter_content(chunk_size=65536):
                    total += len(morceau)
                    if total > borne:
                        raise AuthzDenied(413, "image_too_large")
                    morceaux.append(morceau)
                return b"".join(morceaux)
            finally:
                r.close()
    raise AuthzDenied(502, "fetch_failed")


def _avatar_import(ctx: ResolvedCtx, inp: AvatarImportInput) -> dict:
    from .. import media_store
    _adresse_de_photo(inp.url)
    actuel = (db.get_user(ctx.sub) or {}).get("avatar_url")
    if actuel:
        return {"ok": True, "imported": False, "avatar_url": actuel}
    donnees = _telecharger_photo(inp.url)
    try:
        neuve = media_store.upload_image("avatars", ctx.sub, donnees, "")
    except media_store.MediaError as e:
        raise AuthzDenied(e.status, e.code) from None
    if db.set_avatar_url(ctx.sub, neuve, seulement_si_absent=True):
        return {"ok": True, "imported": True, "avatar_url": neuve}
    # Un upload est arrivé entre la lecture et l'écriture : il gagne, la copie part.
    media_store.delete_by_url(neuve)
    return {"ok": True, "imported": False,
            "avatar_url": (db.get_user(ctx.sub) or {}).get("avatar_url")}


def _org_logo_clear(ctx: ResolvedCtx, inp: OrgLogoClearInput) -> dict:
    """⚠️ Ordre des refus PRÉSERVÉ : id illisible (400) → org inconnue (404) → non-admin
    (403). Une règle d'autz déclarée (`ORG_ADMIN_OF`) rendrait 403 sur une org inconnue,
    là où cette route rend 404 depuis toujours."""
    from .. import roles
    try:
        org_id = int(inp.org_id)
    except (TypeError, ValueError):
        raise AuthzDenied(400, "invalid_id")
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org")
    if not roles.is_org_admin(ctx.sub, org_id):
        raise AuthzDenied(403, "forbidden")
    old = (org_store.get_org(org_id) or {}).get("logo_url")
    org_store.set_org_logo(org_id, None)
    if old:
        from .. import media_store
        media_store.delete_by_url(old)
    return {"ok": True}


def _files_list(ctx: ResolvedCtx, inp: ProjectFilesListInput) -> dict:
    if not db.get_project_by_id(inp.project_id):
        raise AuthzDenied(404, "unknown_project")
    _org_context(ctx, inp.project_id)
    return {"files": [_signed(r) for r in db.list_project_files(inp.project_id)]}


def _file_delete(ctx: ResolvedCtx, inp: ProjectFileInput) -> dict:
    from .. import media_store
    existing = _fichier(ctx, inp.project_id, inp.file_id)
    db.delete_project_file(inp.file_id)
    media_store.delete_by_key(existing["s3_key"])
    db.log_project_activity(inp.project_id, ctx.sub, "project.file_delete",
                            existing.get("title") or existing.get("filename"))
    return {"ok": True}


def _file_public(ctx: ResolvedCtx, inp: ProjectFilePublicInput) -> dict:
    """Bascule le partage public d'un fichier (ADR 0032 §3, B4b) : ACL S3 public-read ↔
    private, URL publique permanente persistée."""
    from .. import media_store
    existing = _fichier(ctx, inp.project_id, inp.file_id)
    make_public = bool(inp.public)
    if make_public:
        # Ouvrir au web exige d'appartenir au propriétaire du projet (#1176) ; refermer
        # réduit la portée et reste ouvert à qui écrit.
        _publication.exiger_appartenance(ctx.sub, inp.project_id)
    # La bascule S3 d'ABORD, la base ENSUITE : la ligne ne dit « public » ou « privé »
    # que si l'ACL a effectivement bougé. Les DEUX sens lèvent la même `MediaError`
    # (cf. `media_store.make_private`) — une ACL refusée est un refus rendu au client,
    # jamais un `{"ok": true}` sur un fichier resté ouvert.
    #
    # ⚠️ La fermeture était hors du `try` (jusqu'au 15/09/2026). La propriété tenait
    # quand même — l'exception coupe avant l'écriture — mais son échec remontait en
    # ERREUR INTERNE au lieu d'un refus nommé. Sur un geste de confidentialité, c'est la
    # pire réponse possible : celui qui referme un partage et n'y arrive pas ne sait pas
    # ce qu'il laisse derrière lui. Banc : `tests/test_media_private_acl_bruyant.py`.
    try:
        if make_public:
            public_url = media_store.make_public(existing["s3_key"])
        else:
            public_url = None
            media_store.make_private(existing["s3_key"])
    except media_store.MediaError as e:
        raise AuthzDenied(e.status, e.code, str(e))
    row = db.set_project_file_public(inp.file_id, make_public, public_url)
    db.log_project_activity(inp.project_id, ctx.sub, "project.file_public",
                            f"{existing.get('title') or existing.get('filename')}:{make_public}")
    return {"ok": True, "file": _signed(row)}


_D_AVATAR = ("Efface mon avatar. L'objet stocké est supprimé avec la référence — pas "
             "d'orphelin dans le stockage.")
_D_AVATAR_IMPORT = (
    "Importe ma photo de profil Google comme avatar, si je n'en ai AUCUN. `url` : "
    "l'adresse `picture` de mon jeton d'identité, sur un hôte d'images de Google (https, "
    "`*.googleusercontent.com`) — toute autre adresse est refusée, et aucune "
    "redirection n'en sort. L'image est récupérée et ré-hébergée. Si j'ai déjà un "
    "avatar, rien ne change : `imported: false` et l'avatar actuel.")
_D_LOGO = ("Efface le logo UPLOADÉ de l'org (org_admin). ⚠️ Le logo AFFICHÉ ne disparaît "
           "pas forcément : il retombe sur celui dérivé du domaine de marque déclaré. "
           "C'est l'upload qu'on retire, pas l'affichage.")
_D_LIST = ("Les fichiers bruts attachés à un projet (« Autre document »). Chaque ligne "
           "porte une `download_url` **signée et temporaire** — elle expire, on ne la "
           "met pas en cache. `public_url`, elle, est permanente tant que le partage est "
           "ouvert. Lecture bornée à l'org de consultation : un projet visible via une "
           "AUTRE de mes orgs rend 404.")
_D_DELETE = ("Supprime un fichier d'un projet (accès écriture). L'objet stocké part avec "
             "la ligne.")
_D_PUBLIC = ("Ouvre ou ferme le partage public d'un fichier. `public` est REQUIS : un "
             "corps sans lui est refusé plutôt que traité comme « rendre privé », parce "
             "qu'un corps mal formé ne doit pas départager un fichier en silence. Ouvrir "
             "exige d'être membre de l'org propriétaire du projet (de l'org parente pour "
             "un projet d'équipe, d'en être le propriétaire pour un projet personnel), "
             "sinon 403 `publish_requires_membership` ; fermer reste ouvert à qui écrit.")

CAPABILITIES += [
    Capability(
        key="me.avatar.clear", handler=_avatar_clear, Input=AvatarClearInput,
        authz=SUB_ONLY, Output=Cleared, description=_D_AVATAR, mcp=None,
        rest=RestBinding("DELETE", "/api/me/avatar"),
    ),
    Capability(
        key="me.avatar.import", handler=_avatar_import, Input=AvatarImportInput,
        authz=SUB_ONLY, Output=AvatarImported, description=_D_AVATAR_IMPORT, mcp=None,
        rest=RestBinding("POST", "/api/me/avatar/import"),
        errors=(
            DeclaredError(400, "url_not_allowed",
                          "l'adresse n'est pas une image de Google en https, ou résout "
                          "vers une adresse interne"),
            DeclaredError(400, "unsupported_type", "l'adresse ne sert pas une image"),
            DeclaredError(413, "image_too_large",
                          "au-delà de la borne d'image (2 Mo par défaut)"),
            DeclaredError(502, "fetch_failed", "l'image n'a pas pu être récupérée"),
        ),
    ),
    Capability(
        key="org.logo.clear", handler=_org_logo_clear, Input=OrgLogoClearInput,
        authz=SUB_ONLY, Output=Cleared, description=_D_LOGO, mcp=None,
        rest=RestBinding("DELETE", "/api/orgs/{id}/logo", path_map={"id": "org_id"}),
    ),
    Capability(
        key="me.project_file.list", handler=_files_list, Input=ProjectFilesListInput,
        authz=SUB_ONLY, Output=ProjectFileList, description=_D_LIST, mcp=None,
        rest=RestBinding("GET", _FILES),
    ),
    Capability(
        key="me.project_file.delete", handler=_file_delete, Input=ProjectFileInput,
        authz=SUB_ONLY, Output=Cleared, description=_D_DELETE, mcp=None,
        rest=RestBinding("DELETE", _FILE),
    ),
    Capability(
        key="me.project_file.set_public", handler=_file_public,
        Input=ProjectFilePublicInput, authz=SUB_ONLY, Output=ProjectFileSaved,
        description=_D_PUBLIC, errors=(_publication.REFUS_HORS_ORG,), mcp=None,
        rest=RestBinding("POST", _FILE + "/public"),
    ),
]
