"""Pennylane (cabinet) — l'API « Firm » de Pennylane, côté cabinet comptable.

Adaptateur MINCE sur `oto.tools.pennylane_firm.PennylaneFirmClient` (oto-core) : un
jeton de cabinet, posé par un admin d'org sur la carte du connecteur (palier org),
atteint toutes les sociétés du portefeuille. Quatre outils :

- `pennylane_firm_companies` — le portefeuille (une page), ou la fiche d'une société ;
- `pennylane_firm_tree` — l'arbre de la GED d'une société : dossiers, ou fichiers
  (filtre `parent_folder_id`), au curseur ;
- `pennylane_firm_create_folder` — ÉCRITURE ;
- `pennylane_firm_upload_url` — frappe le lien du RELAIS d'upload
  (`tools/pennylane_firm_relais.py`, route `POST /api/relay/{token}`), gardé par le
  contrôle de doublon ;
- `pennylane_firm_upload_urls` — la même frappe PAR LOTS (≤ 100) : les liens partent
  dans un MANIFESTE (`GET /api/relay/batch/{token}`), jamais dans la réponse, et le
  `SCRIPT_LOT` fourni les consomme (mesure du 12/10 : ~6,7 s de frappe par fichier à
  l'unité, contre 0,87 s de transfert).

⚠️ `company_id` est l'id CÔTÉ CABINET : rien à voir avec les ids du connecteur
`pennylane` (API Company), et les mêmes que la GED vue par `pennylaneged`. L'API n'a
ni suppression, ni déplacement, ni renommage : un mauvais dépôt ne se corrige pas par
elle — d'où le refus du doublon AVANT d'écrire.

Les erreurs de la lib deviennent des refus nommés (`pennylane_firm_socle.traduire`) ;
un 403 nomme le scope qui manque au jeton. Tous les outils sont synchrones (`def`) :
FastMCP les joue dans un thread, coffre et base compris.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from pydantic import BaseModel

from .. import access, output_projection
from ..auth.hooks import current_user_sub_from_token
from ..connectors import verify as connector_verify
from . import pennylane_firm_relais as relais
from . import pennylane_firm_socle as socle
from .pennylane_firm_socle import Refus, _client

# Ce que la vue par défaut d'une liste retire (`full=True` rend le brut). Denylist :
# un champ neuf de Pennylane reste visible tant qu'on ne l'a pas jugé.
_SOCIETE_RETIREE = ("billing_company_name", "address", "city", "postal_code",
                    "activity_nomenclature", "activity_code")
_GED_RETIREE = ("url", "files", "created_at", "updated_at")


class ElementDeLot(BaseModel):
    """Un fichier d'un lot : son dossier cible et son nom dans la GED.

    Un modèle pydantic et non un `TypedDict` : pydantic refuse celui de `typing` sous
    Python < 3.12, et celui de `typing_extensions` serait une dépendance non déclarée."""
    parent_folder_id: int
    name: str


#: Le script qui consomme un lot : bash + coreutils + awk + curl (et xargs). Constant :
#: l'URL du manifeste est son argument, jamais recopiée dedans. Il joint `paths.tsv`
#: (dossier, nom, chemin local) au manifeste sur (dossier, nom), envoie au plus 4
#: fichiers à la fois (la concurrence du relais par processus) avec des séparateurs NUL
#: (chemins à espaces, accents, `;`, `,`), réessaie le même lien sur un 503
#: `relay_busy` (non consommé), et écrit `results.tsv` ; une ligne sans lien
#: (refusée, ou déjà envoyée) y sort en `NO_LINK`, sans envoi. Le chemin est passé à
#: `-F` entre guillemets DOUBLES, `"` et `\` échappés : sinon curl lit `;` et `,`.
SCRIPT_LOT = r"""#!/usr/bin/env bash
# Uploads a batch of local files through the relay links of an oto manifest.
# usage: bash pennylane_batch.sh '<manifest_url>' [paths.tsv] [results.tsv]
# paths.tsv: one line per file: parent_folder_id<TAB>name<TAB>local path
set -euo pipefail
manifest_url="${1:?usage: bash pennylane_batch.sh <manifest_url> [paths.tsv] [results.tsv]}"
paths="${2:-paths.tsv}"
results="${3:-results.tsv}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
code="$(curl -sS -o "$work/manifest.tsv" -w '%{http_code}' "$manifest_url")" || true
if [ "$code" != "200" ]; then
  echo "manifest download failed (HTTP ${code:-000}):" >&2
  cat "$work/manifest.tsv" >&2 2>/dev/null || true
  exit 1
fi
printf 'code_http\tname\tpath\n' > "$results"
awk -F '\t' -v OFS='\t' -v nolink="$results" '
  { sub(/\r$/, "") }
  $1 !~ /^[0-9]+$/ || NF < 3 { next }
  FILENAME == ARGV[1] { url[($1 + 0) SUBSEP $2] = $3; next }
  { k = ($1 + 0) SUBSEP $2
    if (k in url) { print url[k], $2, $3; delete url[k] }
    else print "NO_LINK", $2, $3 >> nolink }
' "$work/manifest.tsv" "$paths" > "$work/jobs.tsv"
tr '\t\n' '\0\0' < "$work/jobs.tsv" | xargs -0 -r -n 3 -P 4 bash -c '
  url="$1"; name="$2"; path="$3"
  p="${path//\\/\\\\}"; p="${p//\"/\\\"}"
  for try in 1 2 3 4 5 6 7 8 9 10 11 12; do
    code="$(curl -sS -o /dev/null -w "%{http_code}" -F "file=@\"$p\"" "$url")" || true
    [ "$code" = "503" ] || break
    sleep 5
  done
  printf "%s\t%s\t%s\n" "${code:-000}" "$name" "$path"
' _ >> "$results"
awk -F '\t' 'NR > 1 { n[$1]++ } END { for (c in n) printf "%s: %d\n", c, n[c] }' "$results" >&2
echo "details: $results" >&2
"""


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » : une page d'un élément du portefeuille. Prouve
    le jeton ET le scope `companies:readonly` ; un 403 nomme le scope manquant."""
    from oto.tools.pennylane_firm import PennylaneFirmClient

    PennylaneFirmClient(fields["key"]).list_companies(per_page=1)


def _sub() -> str:
    sub = current_user_sub_from_token()
    if not sub:
        raise socle.vers_mcp(Refus(401, "unauthenticated",
                                   "Pennylane (firm) tools need an authenticated caller."))
    return sub


def _appel(appel, geste: str):
    """Un appel à la lib, ses refus traduits en `McpError` nommée."""
    try:
        return socle.traduire(appel, geste)
    except Refus as e:
        raise socle.vers_mcp(e) from e


def _slim(page: dict, retire: tuple, full: bool) -> dict:
    """La vue de tri d'une page (`items`), ou le brut si `full`."""
    if full or not isinstance(page, dict):
        return page
    out = output_projection.project(page, items_path="items", item_drop=retire)
    out["projection"] = {"omitted": list(retire), "full": "pass full=true for every field"}
    return out


def _appelant() -> tuple[str, int]:
    """`(sub, org)` de l'APPEL : le lien de relais scelle les deux, et l'org porte le
    jeton de cabinet que le relais résoudra."""
    sub = _sub()
    org = access.current_org(sub)
    if org is None:
        raise socle.vers_mcp(Refus(
            403, "no_organization",
            "An upload link is minted for an organization: call within one (`_org`)."))
    return sub, org


def register(mcp: FastMCP) -> None:
    connector_verify.register(socle.CONNECTOR, _verify)

    @mcp.tool()
    def pennylane_firm_companies(company_id: Optional[int] = None,
                                 page: Optional[int] = None,
                                 per_page: Optional[int] = None,
                                 client_code: Optional[str] = None,
                                 full: bool = False) -> dict:
        """The accounting firm's portfolio in Pennylane (firm API token): one page of
        companies, or the record of one company with `company_id`. The `id` of a
        company is the firm-side `company_id` every other `pennylane_firm_*` tool
        takes — unrelated to the ids of the `pennylane` connector, the same as
        `pennylaneged`'s. Paged by number: ask page + 1 while it is below
        `total_pages`. 5 requests/s per token; on `pennylane_rate_limited`, wait 60 s.

        Args:
            company_id: firm-side id — returns that one company's record.
            page: page number, from 1.
            per_page: up to 1000.
            client_code: only the company with this client code.
            full: every field (default: id, name, siren, client_code, external_id).
        """
        c = _client()
        if company_id is not None:
            return _appel(lambda: c.get_company(company_id), "the company record")
        filtre = ([{"field": "client_code", "operator": "eq", "value": client_code}]
                  if client_code else None)
        res = _appel(lambda: c.list_companies(page=page, per_page=per_page,
                                              filter=filtre), "the list of companies")
        return _slim(res, _SOCIETE_RETIREE, full)

    @mcp.tool()
    def pennylane_firm_tree(company_id: int,
                            kind: Literal["folders", "files"] = "files",
                            parent_folder_id: Optional[int] = None,
                            cursor: Optional[str] = None,
                            limit: Optional[int] = None,
                            full: bool = False) -> dict:
        """One company's document store (GED) in Pennylane: its folders, or its files
        (one folder's with `parent_folder_id`). Items carry `id`, `name`, `path` and
        `parent_folder`; the tree is rebuilt from them. Paged by cursor: while
        `has_more`, pass `next_cursor` back as `cursor`. 5 requests/s per token.

        Args:
            company_id: firm-side id, from `pennylane_firm_companies`.
            kind: "files" (default) or "folders".
            parent_folder_id: files only — the files of this folder.
            cursor: `next_cursor` of the previous page.
            limit: items per page, up to 100.
            full: every field, download urls and dates included.
        """
        c = _client()
        if kind == "folders":
            if parent_folder_id is not None:
                raise socle.vers_mcp(Refus(
                    400, "unsupported_filter",
                    "Pennylane filters folders by id only: list them all and read "
                    "each one's `parent_folder`."))
            res = _appel(lambda: c.list_dms_folders(company_id, limit=limit,
                                                    cursor=cursor), "the folder list")
        else:
            res = _appel(lambda: c.list_dms_files(company_id, limit=limit, cursor=cursor,
                                                  parent_folder_id=parent_folder_id),
                         "the file list")
        return _slim(res, _GED_RETIREE, full)

    @mcp.tool()
    def pennylane_firm_create_folder(company_id: int, name: str,
                                     parent_folder_id: Optional[int] = None) -> dict:
        """Create a folder in one company's GED, at the root or under
        `parent_folder_id`. Pennylane cannot rename, move or delete a folder through
        the API: list the tree first and reuse an existing folder. Token scope
        dms_files:all.

        Args:
            company_id: firm-side id, from `pennylane_firm_companies`.
            name: the folder's name.
            parent_folder_id: the parent folder; omitted = the root.
        """
        c = _client()
        return _appel(lambda: c.create_dms_folder(company_id, name, parent_folder_id),
                      "the folder creation")

    @mcp.tool()
    def pennylane_firm_upload_url(company_id: int, parent_folder_id: int,
                                  name: str) -> dict:
        """Get a single-use link (15 min) to upload ONE local file into a folder of a
        company's GED: run the returned `command` with the file's path. The file
        goes from your machine to oto, which relays it to Pennylane with the firm
        token — no token on your side. Pennylane cannot delete, move or rename a
        file through the API: an upload cannot be undone. Refused when `name`
        already exists in the folder. Token scope dms_files:all.

        Args:
            company_id: firm-side id, from `pennylane_firm_companies`.
            parent_folder_id: the target folder (never the root), from
                `pennylane_firm_tree`.
            name: the file's name in the GED, extension included.
        """
        sub, org = _appelant()
        try:
            nom = relais.valider_nom(name)
            c = _client()
            socle.verifier_absent(c, org, company_id, parent_folder_id, nom)
            lien = relais.frapper(sub, org, company_id, parent_folder_id, nom)
        except Refus as e:
            raise socle.vers_mcp(e) from e
        return {
            **lien,
            "method": "POST",
            # Chemin entre guillemets DOUBLES : sans eux, curl lit un `;` ou une `,`
            # du chemin comme un séparateur d'options de `-F`.
            "command": f"curl -sS -F 'file=@\"<local path>\"' '{lien['url']}'",
            "target": {"company_id": company_id, "parent_folder_id": parent_folder_id,
                       "name": nom},
            "hint": ("Replace <local path> and run the command: the answer is the file "
                     "Pennylane created. Single use; on `pennylane_rate_limited` wait "
                     "60 s and mint a new link."),
        }

    @mcp.tool()
    def pennylane_firm_upload_urls(company_id: int, items: list[ElementDeLot]) -> dict:
        """Mint single-use upload links (15 min) for a BATCH of 1 to 100 local files
        going into folders of one company's GED — the way to upload many files. The
        links are NOT in the answer: they sit in a manifest that the provided
        `script` downloads, so never copy links around. Steps:
        (1) call this tool with up to 100 items;
        (2) write `paths.tsv`, one line per file:
            parent_folder_id<TAB>name<TAB>local path (same folder and name as the item);
        (3) save `script` to a file and run it ONCE:
            `bash <file> '<manifest_url>' paths.tsv` — it downloads the manifest,
            uploads with at most 4 parallel streams and writes `results.tsv`
            (http code<TAB>name<TAB>path; NO_LINK = no link for that line: refused
            here, or already sent);
        (4) check the folder with `pennylane_firm_tree`.
        Each item is checked on its own: an invalid name (tabs and line breaks
        included), a repeat within the batch, an unknown folder or a name already in
        the folder lands in `refused`, the others still get a link. In results: 200 =
        created; 409 = already present; 000 = not sent (unreadable file, network);
        429 = Pennylane's rate limit: wait 60 s, then mint a new batch with ONLY the
        failed lines. Big files on a slow uplink: use smaller batches, every upload
        must start within the 15 minutes. Pennylane cannot delete, move or rename a
        file through the API: an upload is final.
        Token scope dms_files:all.

        Args:
            company_id: firm-side id, from `pennylane_firm_companies`.
            items: 1 to 100 files, each {parent_folder_id: the target folder (never
                the root), from `pennylane_firm_tree`; name: the file's name in the
                GED, extension included}.
        """
        sub, org = _appelant()
        try:
            elements = [ElementDeLot.model_validate(i).model_dump() for i in items or []]
            lot = relais.frapper_lot(_client(), sub, org, company_id, elements)
        except Refus as e:
            raise socle.vers_mcp(e) from e
        if lot["manifest_url"] is None:
            hint = "No file got a link: read `refused`, fix and mint again."
        else:
            hint = ("Write paths.tsv (parent_folder_id<TAB>name<TAB>local path, one "
                    "line per file of `ready`), save `script` to a file, then run "
                    f"once: bash <file> '{lot['manifest_url']}' paths.tsv — and read "
                    "results.tsv. On 429 wait 60 s and mint a batch of the failed lines "
                    "only.")
        return {**lot, "max_bytes": relais.max_bytes(), "script": SCRIPT_LOT,
                "hint": hint}
