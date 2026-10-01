"""SharePoint & OneDrive — les fichiers Microsoft 365 d'une org, via Microsoft Graph.

Credential = une app Entra enregistrée par l'org dans son annuaire (`directory_id`,
`client_id`, `client_secret`), résolue par appel via
`access.resolve_credential_fields("sharepoint")` (ADR 0011). Le jeton porte les
permissions d'APPLICATION que l'admin du tenant a consenties : ce qu'elles ne
couvrent pas, aucun outil ne le voit — un 403 dit « non autorisé », jamais
« n'existe pas ».

**Surface** (un tool par objet, le verbe en `op`) :
- `sharepoint_site` (search/get/drives) — trouver un site, lire ses bibliothèques
  de documents (une bibliothèque = un drive) ;
- `sharepoint_file` (list/get/search/download/upload/create_folder) — les éléments
  d'un drive, désigné par `drive_id` ou par le OneDrive d'un collaborateur
  (`user`) ; un élément par `item_id` ou par `path`. La lecture passe par
  `file_content.render_for_agent` (texte inline, CSV d'un tableur, URL signée
  sinon) ; un document Word ou PowerPoint est converti en PDF par Graph pour que
  son texte se lise.

⚠️ `upload` et `create_folder` ÉCRIVENT dans le SharePoint de l'org ; par défaut
un nom déjà pris est refusé (`conflict="fail"`), jamais écrasé en silence. Aucune
suppression, aucun déplacement, aucun partage : hors de cette surface.
"""
from __future__ import annotations

import base64
import binascii
import mimetypes
from typing import Literal, Optional, Union
from urllib.parse import urlsplit

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

_NAME = "sharepoint"
_CHAMPS = ("directory_id", "client_id", "client_secret")
# Lus en PDF par défaut : Graph les convertit, et c'est le texte du PDF que l'agent
# lit (le binaire Office brut ne se lit pas). Un tableur reste brut : il se rend
# en CSV. `as_pdf` force l'un ou l'autre.
_CONVERTIS = {"doc", "docx", "dot", "dotx", "odt", "rtf", "ppt", "pptx", "pps",
              "ppsx", "odp"}
_DOWNLOAD_MAX = 50 * 1024 * 1024
_UPLOAD_MAX = 25 * 1024 * 1024
# Codes AADSTS qui disent quel champ de la carte est faux.
_AADSTS = {
    "AADSTS90002": "annuaire introuvable — vérifie `directory_id`",
    "AADSTS900023": "`directory_id` mal formé — un GUID ou un domaine onmicrosoft.com",
    "AADSTS700016": "application introuvable dans ce tenant — vérifie `client_id` "
                    "(et qu'il appartient bien à cet annuaire)",
    "AADSTS7000215": "secret invalide — colle la VALEUR du secret client, pas son ID",
    "AADSTS7000222": "secret expiré — crée un nouveau secret client et remplace-le",
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _champs(fields: dict) -> dict:
    """Les trois champs de la carte, NON VIDES. Un champ vide passé au client y
    lèverait `MissingCredential` au nom de la lib : on le refuse ici, au nom du
    connecteur."""
    vides = [n for n in _CHAMPS if not (fields.get(n) or "").strip()]
    if vides:
        raise ValueError(f"credential SharePoint incomplet : {', '.join(vides)} vide(s)")
    return {n: fields[n].strip() for n in _CHAMPS}


def _graph(champs: dict):
    """Le client Graph de ces champs (l'annuaire en premier argument)."""
    from oto.tools.microsoft import GraphClient

    return GraphClient(champs["directory_id"], champs["client_id"], champs["client_secret"])


def _auth_message(e) -> str:
    raison = _AADSTS.get(e.code or "")
    return (f"Microsoft refuse l'app Entra de la carte SharePoint : {raison}." if raison
            else f"Microsoft refuse l'app Entra de la carte SharePoint : {e}")


def _upstream_message(e) -> str:
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    detail = str((body.get("error") or {}).get("message") or e.body or "")[:400]
    if status in (401, 403):
        return (f"Microsoft Graph refuse l'accès (HTTP {status}) : l'app Entra n'a pas "
                "la permission d'application qu'il faut (Sites.Read.All ou "
                "Files.Read.All en lecture, *.ReadWrite.All pour écrire), le "
                "consentement administrateur n'a pas été donné, ou — avec "
                f"Sites.Selected — ce site ne lui a pas été accordé. {detail}").strip()
    if status == 404:
        return f"Microsoft Graph : introuvable (HTTP 404). {detail}".strip()
    if status == 409:
        return (f"Microsoft Graph : un élément porte déjà ce nom (HTTP 409) — "
                f"`conflict=\"rename\"` ou `\"replace\"` pour passer outre. {detail}").strip()
    return f"Microsoft Graph a refusé la requête (HTTP {status}) : {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » : obtenir un jeton applicatif. Couvre `auth`
    SEUL — les permissions consenties varient (Sites.Selected n'ouvre aucun site
    par défaut) : sonder un site ferait passer une app saine pour une app morte."""
    from oto.tools.microsoft import MicrosoftAuthError

    try:
        champs = _champs(fields)
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))
    try:
        _graph(champs).token()
    except MicrosoftAuthError as e:
        if e.status_code in (400, 401, 403):
            raise connector_verify.NonAutorise(_auth_message(e))
        raise RuntimeError(f"Entra HTTP {e.status_code}: {e}")


def _brut(objet: dict) -> dict:
    """`full=True` : l'objet Graph tel que l'amont le livre."""
    return objet


def _site(s: dict) -> dict:
    return {k: s.get(k) for k in ("id", "displayName", "name", "webUrl", "description")}


def _drive(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "name", "driveType", "webUrl", "description")}


def _item(i: dict) -> dict:
    """La vue d'un driveItem : de quoi le reconnaître et le rouvrir."""
    parent = i.get("parentReference") or {}
    dossier = (parent.get("path") or "").split("root:", 1)[-1] if parent.get("path") else None
    return {
        "id": i.get("id"), "name": i.get("name"),
        "kind": "folder" if "folder" in i else "file",
        "size": i.get("size"),
        "mimeType": (i.get("file") or {}).get("mimeType"),
        "childCount": (i.get("folder") or {}).get("childCount"),
        "folder_path": (dossier or "/") if dossier is not None else None,
        "drive_id": parent.get("driveId"),
        "webUrl": i.get("webUrl"),
        "lastModifiedDateTime": i.get("lastModifiedDateTime"),
        "lastModifiedBy": ((i.get("lastModifiedBy") or {}).get("user") or {}).get("displayName"),
    }


def _refuse_ignored(op: str, **provided) -> None:
    """Un argument fourni que CET op n'utilise pas est une erreur d'intention."""
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op='{op}' n'utilise pas `{name}`.")


def _need(value, name: str, op: str):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _bad(f"op='{op}' requiert `{name}`.")
    return value


def _contenu(content_base64: Optional[str], content_text: Optional[str]) -> bytes:
    if (content_base64 is None) == (content_text is None):
        raise _bad("op='upload' requiert `content_base64` OU `content_text` (un seul).")
    if content_text is not None:
        data = content_text.encode("utf-8")
    else:
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise _bad("`content_base64` n'est pas du base64 valide — encode le fichier "
                       "entier, sans en-tête `data:` ni retour à la ligne.") from None
    if not data:
        raise _bad("le contenu à déposer est vide.")
    if len(data) > _UPLOAD_MAX:
        raise _bad(f"fichier de {len(data) // (1024 * 1024)} Mo : ce tool dépose "
                   f"jusqu'à {_UPLOAD_MAX // (1024 * 1024)} Mo.")
    return data


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.microsoft import GraphClient, MicrosoftAuthError

    from .. import file_content

    connector_verify.register(_NAME, _verify)

    def _client() -> GraphClient:
        try:
            return _graph(_champs(access.resolve_credential_fields(_NAME)))
        except ValueError as e:
            raise _bad(str(e))

    def _run(fn):
        """Refus d'Entra ou 4xx de Graph → refus nommé. 429 et 5xx restent ce
        qu'ils sont : la taxonomie d'erreurs les classe réessayables."""
        try:
            return fn()
        except MicrosoftAuthError as e:
            raise _bad(_auth_message(e))
        except UpstreamHTTPError as e:
            if 400 <= e.status_code < 500 and e.status_code != 429:
                raise _bad(_upstream_message(e))
            raise
        except ValueError as e:
            raise _bad(str(e))

    def _drive_id(client: GraphClient, drive_id: Optional[str], user: Optional[str]) -> str:
        if bool(drive_id) == bool(user):
            raise _bad("désigne le drive par `drive_id` (une bibliothèque, depuis "
                       "sharepoint_site op='drives') OU par `user` (le OneDrive de ce "
                       "collaborateur, par son adresse) — un seul des deux.")
        return drive_id or _run(lambda: client.get_user_drive(user))["id"]

    @mcp.tool()
    def sharepoint_site(
        op: Literal["search", "get", "drives"] = "search",
        query: Optional[str] = None,
        site_id: Optional[str] = None,
        url: Optional[str] = None,
        limit: int = 50,
        full: bool = False,
    ) -> dict:
        """A SharePoint site of the organization, and its document libraries.

        `op`:
        - **"search"** (default): sites whose name or description match `query`.
          Only the sites the organization's Entra app can see (with the
          `Sites.Selected` permission, only those granted to it).
        - **"get"**: one site, by `site_id` or by its `url` as read in the
          browser (e.g. https://contoso.sharepoint.com/sites/Marketing).
        - **"drives"**: the site's document libraries (`site_id`). A library is a
          drive: pass its `id` as `drive_id` to `sharepoint_file`.

        Args:
            op: search (default) | get | drives.
            query: op="search" — words to look for.
            site_id: op="get"/"drives" — the site id returned by search/get.
            url: op="get" — the site's address, instead of `site_id`.
            limit: op="search"/"drives" — max results (default 50).
            full: return Graph's raw objects instead of the trimmed view.
        """
        site_vue, drive_vue = (_brut, _brut) if full else (_site, _drive)
        c = _client()
        if op == "search":
            _refuse_ignored(op, site_id=site_id, url=url)
            sites = _run(lambda: c.search_sites(_need(query, "query", op), limit=limit))
            return {"sites": [site_vue(s) for s in sites], "count": len(sites)}
        if op == "get":
            _refuse_ignored(op, query=query)
            if bool(site_id) == bool(url):
                raise _bad("op='get' requiert `site_id` OU `url` (un seul).")
            if site_id:
                return site_vue(_run(lambda: c.get_site(site_id)))
            parts = urlsplit(url.strip())
            if parts.scheme != "https" or not parts.hostname:
                raise _bad("`url` est l'adresse https du site, ex. "
                           "https://contoso.sharepoint.com/sites/Marketing")
            chemin = parts.path.strip("/")
            return site_vue(_run(lambda: c.get_site_by_path(parts.hostname, chemin) if chemin
                              else c.get_site(parts.hostname)))
        if op == "drives":
            _refuse_ignored(op, query=query, url=url)
            drives = _run(lambda: c.list_site_drives(_need(site_id, "site_id", op),
                                                     limit=limit))
            return {"drives": [drive_vue(d) for d in drives], "count": len(drives)}
        raise _bad("op doit être 'search', 'get' ou 'drives'.")

    @mcp.tool()
    def sharepoint_file(
        op: Literal["list", "get", "search", "download", "upload",
                    "create_folder"] = "list",
        drive_id: Optional[str] = None,
        user: Optional[str] = None,
        item_id: Optional[str] = None,
        path: Optional[str] = None,
        query: Optional[str] = None,
        name: Optional[str] = None,
        content_base64: Optional[str] = None,
        content_text: Optional[str] = None,
        conflict: Literal["fail", "rename", "replace"] = "fail",
        as_pdf: Optional[bool] = None,
        sheet: Optional[Union[int, str]] = None,
        max_rows: Optional[int] = None,
        limit: int = 200,
        full: bool = False,
    ) -> dict:
        """A file or folder in a SharePoint document library or a OneDrive.

        The drive is `drive_id` (a library, from `sharepoint_site` op="drives")
        OR `user` (that person's OneDrive, by email) — exactly one. Inside it, an
        item is `item_id` OR `path` relative to the drive root
        ("Contrats/2026/nda.docx"); neither = the drive root.

        `op`:
        - **"list"** (default): the content of a folder (`item_id`/`path`, root
          when omitted). Returns {items: [{id, name, kind, size, mimeType,
          childCount, folder_path, webUrl, lastModifiedDateTime, lastModifiedBy}]}.
        - **"get"**: one item's metadata.
        - **"search"**: files of the whole drive matching `query` (name, metadata
          and content, from SharePoint's index — a file added seconds ago may not
          show yet).
        - **"download"**: READ a file's content. Word and PowerPoint documents are
          converted to PDF by Microsoft and their TEXT returned inline (plus
          `raw_url`); small text files inline; spreadsheets (.xlsx) inline as CSV
          (`sheet`, `max_rows`); anything else as a short-lived signed URL.
          `as_pdf=true` converts another Office type, `as_pdf=false` returns the
          original bytes. Up to 50 MB.
        - **"upload"**: ⚠️ WRITES — drop a file `name` into the folder
          `item_id`/`path` (root when omitted), from `content_base64` (any file)
          or `content_text` (UTF-8 text). Up to 25 MB. An existing name is
          refused by default (`conflict="fail"`); "rename" keeps both,
          "replace" overwrites.
        - **"create_folder"**: ⚠️ WRITES — a folder `name` inside `item_id`/
          `path` (root when omitted), same `conflict` rule.

        Nothing here deletes, moves or shares a file.

        Args:
            op: list (default) | get | search | download | upload | create_folder.
            drive_id: the library's drive id — or `user`.
            user: email of a person whose OneDrive to use — or `drive_id`.
            item_id: the item (folder for list/upload/create_folder) by id.
            path: the item by path from the drive root, instead of `item_id`.
            query: op="search" — words to look for.
            name: op="upload"/"create_folder" — the new file or folder name.
            content_base64: op="upload" — the file, base64-encoded.
            content_text: op="upload" — the file as plain text, instead.
            conflict: op="upload"/"create_folder" — fail (default) | rename |
                replace.
            as_pdf: op="download" — force (true) or skip (false) the PDF
                conversion; omit for the default per type.
            sheet: op="download" of an .xlsx — sheet name or 0-based index.
            max_rows: op="download" of an .xlsx — rows per sheet (default 200).
            limit: op="list"/"search" — max items (default 200).
            full: return Graph's raw driveItems instead of the trimmed view.
        """
        vue = _brut if full else _item
        if (as_pdf is not None or sheet is not None or max_rows is not None) \
                and op != "download":
            raise _bad(f"`as_pdf`/`sheet`/`max_rows` ne valent que pour op='download' "
                       f"(reçu op='{op}').")
        if (content_base64 is not None or content_text is not None) and op != "upload":
            raise _bad(f"`content_base64`/`content_text` ne valent que pour op='upload' "
                       f"(reçu op='{op}').")
        c = _client()
        drive = _drive_id(c, drive_id, user)

        if op == "list":
            _refuse_ignored(op, query=query, name=name)
            items = _run(lambda: c.list_children(drive, item_id=item_id, path=path,
                                                 limit=limit))
            return {"drive_id": drive, "items": [vue(i) for i in items],
                    "count": len(items)}

        if op == "get":
            _refuse_ignored(op, query=query, name=name)
            return vue(_run(lambda: c.get_item(drive, item_id=item_id, path=path)))

        if op == "search":
            _refuse_ignored(op, item_id=item_id, path=path, name=name)
            items = _run(lambda: c.search_items(drive, _need(query, "query", op),
                                                limit=limit))
            return {"drive_id": drive, "items": [vue(i) for i in items],
                    "count": len(items)}

        if op == "download":
            _refuse_ignored(op, query=query, name=name)
            if not item_id and not path:
                raise _bad("op='download' requiert `item_id` ou `path`.")
            meta = _run(lambda: c.get_item(drive, item_id=item_id, path=path))
            if "folder" in meta:
                raise _bad(f"« {meta.get('name')} » est un dossier : op='list' pour "
                           "voir son contenu.")
            if (meta.get("size") or 0) > _DOWNLOAD_MAX:
                raise _bad(f"« {meta.get('name')} » pèse {meta['size'] // (1024 * 1024)} "
                           f"Mo : ce tool lit jusqu'à {_DOWNLOAD_MAX // (1024 * 1024)} Mo "
                           f"(ouvre-le par son webUrl : {meta.get('webUrl')}).")
            nom = meta.get("name") or meta["id"]
            ext = nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
            pdf = as_pdf if as_pdf is not None else ext in _CONVERTIS
            data = _run(lambda: c.download(drive, item_id=meta["id"],
                                           format="pdf" if pdf else None))
            if pdf:
                nom, mime = f"{nom.rsplit('.', 1)[0]}.pdf", "application/pdf"
            else:
                mime = ((meta.get("file") or {}).get("mimeType")
                        or mimetypes.guess_type(nom)[0] or "application/octet-stream")
            sub = access.current_user_sub_or_raise()
            try:
                out = file_content.render_for_agent(
                    data, nom, mime, sub=sub, prefix="sharepoint-files",
                    sheet=sheet, max_rows=max_rows)
            except (file_content.MediaUnavailable, file_content.SpreadsheetError) as e:
                raise _bad(str(e)) from None
            return {**out, "item": vue(meta), **({"converted_from": ext} if pdf else {})}

        if op == "upload":
            _refuse_ignored(op, query=query)
            data = _contenu(content_base64, content_text)
            fichier = _need(name, "name", op)
            mime = mimetypes.guess_type(fichier)[0] or (
                "text/plain; charset=utf-8" if content_text is not None
                else "application/octet-stream")
            return vue(_run(lambda: c.upload(drive, fichier, data, parent_id=item_id,
                                               parent_path=path, conflict=conflict,
                                               content_type=mime)))

        if op == "create_folder":
            _refuse_ignored(op, query=query)
            return vue(_run(lambda: c.create_folder(drive, _need(name, "name", op),
                                                      parent_id=item_id, parent_path=path,
                                                      conflict=conflict)))

        raise _bad("op doit être 'list', 'get', 'search', 'download', 'upload' ou "
                   "'create_folder'.")
