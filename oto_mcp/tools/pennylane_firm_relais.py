"""Le RELAIS d'upload du connecteur `pennylane_firm` — la partie synchrone.

Le poste de l'utilisateur envoie un fichier SANS jeton Pennylane (`curl -F file=@…`
sur une URL signée à usage unique) ; le serveur le relaie à la GED de la société avec
le jeton de cabinet du coffre de l'org, et rend l'objet fichier que Pennylane a créé.
La route (`api/pennylane_firm.py`) lit le multipart ; ici vit tout ce qui touche la
base, le coffre ou Pennylane, appelé depuis un thread (`run_in_threadpool`).

- **Jeton** : celui des uploads signés (`upload_tokens.sign/verify`, HMAC, 15 min,
  `jti` à usage unique dans la même table), sous un `typ` DISTINCT (`relay`) — un
  jeton d'upload oto ne s'ouvre pas ici, un jeton de relais ne s'ouvre pas sur
  `/api/upload/{token}`. Cible scellée : `{kind, company_id, parent_folder_id, name}`
  + `sub` + `org`.
- **Org scellée** (`preparer`) : l'appartenance du `sub` à l'org du jeton est
  revérifiée, puis l'org est ÉPINGLÉE pour résoudre le credential — sans quoi la
  cascade retomberait sur l'org « maison » du compte, qui n'est pas forcément celle
  qui a frappé le lien.
- **Plafond** (`max_bytes`) et **concurrence** (`prendre_place`) : réglages propres au
  relais, distincts de ceux des uploads oto.
- **Aucune connexion base pendant le transfert** : `preparer` lit tout ce qu'il faut
  (appartenance, suspension, activation, coffre, doublon) et rend la main ;
  `relayer` ne parle qu'à Pennylane.
- **Par lots** (`frapper_lot`) : jusqu'à `LOT_MAX` liens d'un coup, chacun le même
  jeton `relay` qu'à l'unité. Les URL ne sont PAS rendues à l'agent (il devrait les
  recopier, ~550 caractères de jeton chacune) : elles vont au MANIFESTE, gardé en
  mémoire du processus (`deposer_manifeste`) et servi en TSV par
  `GET /api/relay/batch/{token}` sous un jeton `relay_batch` distinct.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import config, upload_tokens
from . import pennylane_firm_socle as socle
from .pennylane_firm_socle import Refus, _client

TYP = "relay"
KIND = "pennylane_firm_dms"
TTL_S = 900
NOM_MAX = 255

#: Le jeton du MANIFESTE d'un lot : une famille à part, qui ne s'ouvre ni sur
#: `/api/upload/{token}` ni sur `POST /api/relay/{token}`, et inversement.
TYP_LOT = "relay_batch"
KIND_LOT = "pennylane_firm_manifest"

#: Taille maximale d'un lot. Les liens vivent 15 min (`TTL_S`) : 100 fichiers de
#: 100 Mo (le plafond du relais) sur 4 flux à ~9 Mo/s montent en ~280 s, dans le TTL
#: avec de la marge ; sur une montée plus lente, la description conseille des lots
#: plus petits pour les gros fichiers. Et la réponse comme le manifeste (≈550
#: caractères de jeton par lien, ~55 Ko à 100) restent de taille bornée.
LOT_MAX = 100
#: Manifestes vivants au plus dans le processus. Au-delà, refus nommé réessayable
#: plutôt qu'une éviction silencieuse d'un lot en cours.
MANIFESTES_MAX = 500

#: PROVISOIRE : à relever après mesure de la limite réelle de Pennylane, que sa
#: documentation ne donne pas.
_DEFAUT_MAX_BYTES = 100 * 1024 * 1024
_DEFAUT_CONCURRENCE = 4

#: Le nom stable du dépôt dans le journal d'appels.
OUTIL_JOURNAL = "pennylane_firm_relay"


def max_bytes() -> int:
    raw = os.environ.get("OTO_PENNYLANE_FIRM_UPLOAD_MAX_BYTES")
    return int(raw) if raw else _DEFAUT_MAX_BYTES


def concurrence() -> int:
    raw = os.environ.get("OTO_PENNYLANE_FIRM_RELAY_CONCURRENCY")
    return int(raw) if raw else _DEFAUT_CONCURRENCE


# ── Concurrence : au plus N relais par processus, sans attente ─────────────────────

_PLACES = threading.Lock()
_en_cours = 0


def prendre_place() -> bool:
    """Prend une place de relais, ou rend `False` tout de suite (jamais d'attente :
    un relais tient un fichier sur disque et un thread des minutes durant)."""
    global _en_cours
    with _PLACES:
        if _en_cours >= concurrence():
            return False
        _en_cours += 1
        return True


def rendre_place() -> None:
    global _en_cours
    with _PLACES:
        _en_cours = max(0, _en_cours - 1)


# ── Le jeton ───────────────────────────────────────────────────────────────────────

def valider_nom(name: Optional[str]) -> str:
    nom = (name or "").strip()
    if not 0 < len(nom) <= NOM_MAX:
        raise Refus(400, "invalid_name",
                    f"`name` is the file's name in the GED: 1 to {NOM_MAX} characters.")
    return nom


def frapper(sub: str, org_id: int, company_id: int, parent_folder_id: int,
            name: str) -> dict:
    """Signe le lien de relais. Le contrôle de doublon est joué par l'appelant
    AVANT : un lien ne se frappe que pour un dépôt qui peut réussir."""
    cible = {"kind": KIND, "company_id": int(company_id),
             "parent_folder_id": int(parent_folder_id), "name": name}
    jeton, exp = upload_tokens.sign(sub, org_id, cible, ttl=TTL_S, typ=TYP)
    url = f"{config.public_base_url()}/api/relay/{jeton}"
    return {"url": url, "expires_at": exp, "max_bytes": max_bytes()}


def verifier(jeton: str) -> Optional[dict]:
    """Le payload d'un jeton de RELAIS valide (signature, `typ`, expiration, cible),
    `None` sinon. Ne consomme pas le jeton."""
    payload = upload_tokens.verify(jeton, typ=TYP)
    if payload is None:
        return None
    cible = payload.get("target") or {}
    if cible.get("kind") != KIND or cible.get("parent_folder_id") is None:
        return None
    return payload


# ── Par lots : frappe, et le manifeste qui porte les liens ──────────────────────────

def valider_nom_de_lot(name: Optional[str]) -> str:
    """`valider_nom`, plus ce que le manifeste TSV exige : une tabulation ou un saut de
    ligne dans un nom casserait sa ligne. Refusé plutôt qu'échappé."""
    nom = valider_nom(name)
    if any(c in nom for c in "\t\r\n"):
        raise Refus(400, "invalid_name",
                    "`name` cannot contain a tab or a line break: the batch manifest "
                    "is tab-separated, one line per file.")
    return nom


def _ligne_refusee(dossier, nom, e: Refus) -> dict:
    return {"parent_folder_id": dossier, "name": nom, "code": e.code,
            "message": e.message}


def _dossier_existe(c, company_id: int, dossier: int) -> bool:
    """UNE lecture filtrée par id (forme `eq` + valeur chaîne, celle mesurée en réel).
    L'id rendu est relu : un filtre ignoré en amont ne passerait pas pour « trouvé »."""
    page = socle.traduire(lambda: c.list_dms_folders(
        company_id, filter=[{"field": "id", "operator": "eq", "value": str(dossier)}]),
        f"the lookup of folder {dossier}")
    return any(str(it.get("id")) == str(dossier) for it in page.get("items") or [])


def frapper_lot(c, sub: str, org_id: int, company_id: int, items: list) -> dict:
    """Contrôle chaque élément, frappe un lien par élément accepté, dépose le
    manifeste. Un refus PROPRE à un élément le range dans `refused` sans faire échouer
    le lot ; une erreur de Pennylane qui ne l'est pas (429, 401, scope, 5xx, société
    introuvable) lève son `Refus` AVANT toute frappe. Synchrone (réseau)."""
    if not items:
        raise Refus(400, "empty_batch", f"`items` holds 1 to {LOT_MAX} files.")
    if len(items) > LOT_MAX:
        raise Refus(400, "batch_too_large",
                    f"{len(items)} files: a batch holds {LOT_MAX} at most (the links "
                    f"live {TTL_S // 60} minutes). Split it.")
    refuses: dict[int, dict] = {}
    vivants: list[tuple[int, int, str]] = []   # (rang, dossier, nom)
    vus: set[tuple[int, str]] = set()
    for rang, it in enumerate(items):
        dossier, brut = int(it["parent_folder_id"]), it.get("name")
        try:
            nom = valider_nom_de_lot(brut)
        except Refus as e:
            refuses[rang] = _ligne_refusee(dossier, brut, e)
            continue
        if (dossier, nom) in vus:
            refuses[rang] = _ligne_refusee(dossier, nom, Refus(
                400, "duplicate_in_batch",
                "The same name and folder appear earlier in this batch: only the first "
                "one gets a link."))
            continue
        vus.add((dossier, nom))
        vivants.append((rang, dossier, nom))

    dossiers = list(dict.fromkeys(d for _, d, _ in vivants))
    absents = {d for d in dossiers if not _dossier_existe(c, company_id, d)}
    incomplets: dict[int, Refus] = {}
    acceptes: list[tuple[int, int, str]] = []
    for rang, dossier, nom in vivants:
        if dossier in absents:
            refuses[rang] = _ligne_refusee(dossier, nom, Refus(
                404, "folder_not_found",
                f"No folder {dossier} in company {company_id}: take its id from "
                "`pennylane_firm_tree` (kind=folders)."))
            continue
        if dossier in incomplets:
            refuses[rang] = _ligne_refusee(dossier, nom, incomplets[dossier])
            continue
        try:
            socle.verifier_absent(c, org_id, company_id, dossier, nom)
        except Refus as e:
            if e.code == "duplicate_check_incomplete":
                incomplets[dossier] = e
            elif e.code != "name_already_exists":
                raise
            refuses[rang] = _ligne_refusee(dossier, nom, e)
            continue
        acceptes.append((rang, dossier, nom))

    pret = [{"parent_folder_id": d, "name": n} for _, d, n in acceptes]
    refuse = [refuses[r] for r in sorted(refuses)]
    if not acceptes:
        return {"manifest_url": None, "expires_at": None, "ready": pret,
                "refused": refuse}
    lignes, expire = [], 0
    for _, dossier, nom in acceptes:
        lien = frapper(sub, org_id, company_id, dossier, nom)
        lignes.append((dossier, nom, lien["url"]))
        expire = max(expire, int(lien["expires_at"]))
    manifest_id = deposer_manifeste(sub, org_id, lignes, expire)
    jeton, exp = upload_tokens.sign(sub, org_id, {"kind": KIND_LOT,
                                                  "manifest_id": manifest_id},
                                    ttl=TTL_S, typ=TYP_LOT)
    return {"manifest_url": f"{config.public_base_url()}/api/relay/batch/{jeton}",
            "expires_at": exp, "ready": pret, "refused": refuse}


# Le magasin des manifestes : EN MÉMOIRE du processus (le serveur est mono-processus).
# Un redémarrage les perd — assumé : la route le dit (« mint the batch again »), et
# les liens eux-mêmes ne sont rien de plus que des jetons signés, refrappables.
_MANIFESTES_VERROU = threading.Lock()
# id -> {"sub", "org", "expire": epoch, "lignes": [(dossier, nom, url)]}
_MANIFESTES: dict[str, dict] = {}


def deposer_manifeste(sub: str, org_id: int, lignes: list, expire: int) -> str:
    """Garde les lignes d'un lot sous un id aléatoire jusqu'à `expire` (epoch, celle
    des liens). Purge les expirés à chaque écriture ; plein, refuse (réessayable)."""
    with _MANIFESTES_VERROU:
        maintenant = time.time()
        for mid in [m for m, e in _MANIFESTES.items() if e["expire"] <= maintenant]:
            del _MANIFESTES[mid]
        if len(_MANIFESTES) >= MANIFESTES_MAX:
            raise Refus(503, "manifest_store_full",
                        f"{MANIFESTES_MAX} batches are already waiting on this server: "
                        "retry in a minute.", retryable=True, retry_after=60)
        mid = secrets.token_urlsafe(16)
        _MANIFESTES[mid] = {"sub": sub, "org": org_id, "expire": expire,
                            "lignes": list(lignes)}
    return mid


def verifier_manifeste(jeton: str) -> Optional[dict]:
    """Le payload d'un jeton de MANIFESTE valide (signature, `typ`, expiration, cible),
    `None` sinon."""
    payload = upload_tokens.verify(jeton, typ=TYP_LOT)
    if payload is None:
        return None
    cible = payload.get("target") or {}
    if cible.get("kind") != KIND_LOT or not cible.get("manifest_id"):
        return None
    return payload


def lire_manifeste(sub: str, org_id: Optional[int], manifest_id: str) -> str:
    """Le manifeste en TSV (`parent_folder_id`, `name`, `url`), pour le compte et l'org
    SCELLÉS. Rejoue l'appartenance (base) : synchrone, appelé hors boucle. Lectures
    répétées permises tant qu'il vit ; les liens, eux, restent à usage unique."""
    from .. import roles

    if org_id is None:
        raise Refus(403, "no_organization",
                    "This manifest was minted outside any organization: mint the batch "
                    "again within the organization that holds the firm token.")
    if not roles.is_org_member(sub, org_id):
        raise Refus(403, "not_an_org_member",
                    f"The account that minted this batch is no longer a member of "
                    f"organization {org_id}.")
    with _MANIFESTES_VERROU:
        e = _MANIFESTES.get(manifest_id)
        vivant = (e is not None and e["expire"] > time.time() and e["sub"] == sub
                  and e["org"] == org_id)
        lignes = list(e["lignes"]) if vivant else None
    if lignes is None:
        raise Refus(404, "manifest_expired",
                    "This batch manifest is gone (15 minutes passed, or the server "
                    "restarted): mint the batch again.")
    return "".join(f"{d}\t{n}\t{u}\n" for d, n, u in
                   [("parent_folder_id", "name", "url"), *lignes])


# ── Préparer (base, coffre) puis relayer (Pennylane seul) ──────────────────────────

@dataclass
class Preparation:
    sub: str
    org_id: int
    company_id: int
    parent_folder_id: int
    name: str
    key_mode: Optional[str] = None
    # Le jeton de cabinet : jamais rendu par `repr` (une trace d'exception le
    # recopierait sinon dans un journal).
    key: str = field(default="", repr=False)


def preparer(sub: str, org_id: Optional[int], cible: dict) -> Preparation:
    """Rejoue l'org SCELLÉE, résout le jeton, applique le contrôle de doublon.
    Synchrone : appelé hors boucle. Lève `Refus`."""
    from .. import access, org_suspension, roles, session_org
    from ..connectors import activation
    from ..mcp_errors import McpError

    if org_id is None:
        raise Refus(403, "no_organization",
                    "This link was minted outside any organization: mint another one "
                    "within the organization that holds the firm token.")
    if not roles.is_org_member(sub, org_id):
        raise Refus(403, "not_an_org_member",
                    f"The account that minted this link is no longer a member of "
                    f"organization {org_id}.")
    if (texte := org_suspension.refus(org_id)):
        raise Refus(403, "org_suspended", texte)
    if activation.cran_qui_coupe(socle.CONNECTOR, org_id, None) is not None:
        raise Refus(403, "connector_disabled",
                    f"The connector `{socle.CONNECTOR}` is disabled for organization "
                    f"{org_id}.")
    epingle = session_org.set_call_org(org_id)
    try:
        rc = access.resolve_credential(socle.CONNECTOR, sub=sub)
    except McpError as e:
        detail = getattr(getattr(e, "error", None), "message", None) or str(e)
        raise Refus(424, "credential_unavailable",
                    f"No firm token reachable for organization {org_id}: {detail}") from e
    finally:
        session_org.reset_call_org(epingle)
    company_id, dossier = int(cible["company_id"]), int(cible["parent_folder_id"])
    nom = valider_nom(cible.get("name"))
    c = _client(rc.key)
    socle.verifier_absent(c, org_id, company_id, dossier, nom)
    return Preparation(sub=sub, org_id=int(org_id), company_id=company_id,
                       parent_folder_id=dossier, name=nom, key_mode=rc.mode, key=rc.key)


def relayer(prep: Preparation, fichier, filename: Optional[str],
            content_type: Optional[str]) -> dict:
    """Envoie `fichier` (objet fichier SEEKABLE, lu en flux par la lib, jamais
    chargé en entier) à la GED. Réserve le nom, le confirme au succès, le libère à
    l'échec ; un 422 invalide l'entrée du cache. Ne touche pas la base."""
    c = _client(prep.key)
    socle.reserver(c, prep.org_id, prep.company_id, prep.parent_folder_id, prep.name)
    reussi = False
    try:
        fichier.seek(0)
        resultat = socle.traduire(lambda: c.upload_dms_file(
            prep.company_id, fichier, filename or prep.name, prep.parent_folder_id,
            name=prep.name, content_type=content_type), "the upload")
        reussi = True
        return resultat
    except Refus as e:
        if e.status == 422:
            socle.invalider(prep.org_id, prep.company_id, prep.parent_folder_id)
        raise
    finally:
        if reussi:
            socle.confirmer(prep.org_id, prep.company_id, prep.parent_folder_id,
                            prep.name)
        else:
            socle.liberer(prep.org_id, prep.company_id, prep.parent_folder_id,
                          prep.name)


def ligne_journal(payload: dict, *, ok: bool, error: Optional[str],
                  duree_ms: int, octets: Optional[int],
                  file_id=None, key_mode: Optional[str] = None) -> dict:
    """La ligne `tool_calls` d'un dépôt. `kind='mcp'` : le dépôt est la fin du geste
    d'un agent (`pennylane_firm_upload_url`), et `connector` désigne les ÉCHECS de
    résolution de credential — une ligne réussie y passerait pour une panne. Jamais
    le jeton, ni de Pennylane ni du lien."""
    cible = payload.get("target") or {}
    args = {"company_id": cible.get("company_id"),
            "parent_folder_id": cible.get("parent_folder_id"),
            "name": cible.get("name"), "bytes": octets}
    if file_id is not None:
        args["file_id"] = file_id
    return {"kind": "mcp", "tool": OUTIL_JOURNAL, "sub": payload.get("sub"),
            "org_id": payload.get("org"), "args": args, "ok": ok, "error": error,
            "duration_ms": duree_ms, "key_mode": key_mode}
