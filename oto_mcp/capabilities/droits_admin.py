"""Lire les droits déclarés depuis la console d'administration (oto-app#13, lot A).

Un admin plateforme doit pouvoir répondre à « qui a quel droit, posé par qui, jusqu'à
quand ? » sans ouvrir les orgs une par une — et voir sur la fiche d'une org, ou d'un
compte, les droits qui s'y appliquent, échus compris. Trois lectures, une seule forme
de ligne (`EntitlementRow`) :

- la liste transversale, filtrée et paginée (`GET /api/admin/entitlements`, et
  `oto_admin_org op=entitlements`) : lignes d'org, de personne dans une org, de
  personne partout ;
- les droits d'une org, sur sa fiche admin (`entitlements` à côté de `billing`) ;
- les droits posés sur une personne, sur sa fiche admin.

**Tout se lit dans `org_entitlements`** (ADR 0070 §7), par `db/entitlements
.lignes_filtrees` : une requête, jointe aux orgs et aux comptes pour leurs libellés.
Rien ne lit les dons d'option, les abonnements ni la facturation du cœur, qui partent
avec son retrait (oto-backend#1104) ; un banc l'exige (`tests/test_droits_admin_live.py`).
Ces lectures n'appliquent rien : la valeur qu'un point d'usage applique reste celle de
`access.valeur_expliquee`.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .. import entitlements_catalogue as catalogue
from ..db import entitlements as db_entitlements
from ._authz import PLATFORM_ADMIN
from ._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding,
                     cap_limit)
from .registry import CAPABILITIES

#: Taille de page par défaut et plafond (écrêté, jamais refusé : `cap_limit`).
PAGE = 100
PAGE_MAX = 500

Portee = Literal["org", "personne_dans_org", "personne_partout"]
PorteeFiltre = Literal["org", "personne_dans_org", "personne_partout", "personne"]

# Ce que la vue resserrée (le défaut de la liste) retire de chaque ligne : les
# libellés joints et la signature. `full=true` les rend ; les fiches les rendent
# toujours.
_COMPLETS = ("org_name", "org_created_at", "email", "granted_by", "granted_at")


class EntitlementRow(BaseModel):
    """Une ligne de droit déclaré (`org_entitlements`). Dates en
    `"YYYY-MM-DD HH:MM:SS"`, UTC (fabrique de lignes de `db/_conn`)."""
    org_id: Optional[int] = Field(None, description="L'org où le droit s'applique ; "
                                                    "null = la personne partout.")
    sub: Optional[str] = Field(None, description="La personne ; null = ligne de l'org.")
    scope: Portee = Field(description="org | personne_dans_org | personne_partout")
    right_key: str
    value: int = Field(description="Oui/non = 1/0, sinon le nombre ; 2147483647 = "
                                   "sans plafond.")
    source: str = Field(description="L'étiquette du producteur (opaque pour le cœur).")
    starts_at: str
    expires_at: Optional[str] = Field(None, description="Fin EXCLUE ; null = sans "
                                                        "échéance.")
    active: bool = Field(description="Valide à l'instant de la base : début atteint, "
                                     "fin non atteinte.")
    # Vue complète seulement (`full=true` sur la liste ; toujours sur les fiches).
    org_name: Optional[str] = None
    org_created_at: Optional[str] = None
    email: Optional[str] = None
    granted_by: Optional[str] = Field(None, description="Qui l'a posé (`service:<client>` "
                                                        "pour le commerce).")
    granted_at: Optional[str] = Field(None, description="Dernière pose de la ligne.")


class EntitlementsPage(BaseModel):
    """Une page de droits, dans un ordre total et stable (org, personne, droit,
    source ; lignes de personne partout en tête). `next_cursor` = la page suivante,
    null = c'était la dernière."""
    entitlements: list[EntitlementRow]
    count: int
    next_cursor: Optional[str] = None


def _ligne(r: dict, *, full: bool) -> dict:
    out = {"org_id": r["org_id"], "sub": r["sub"], "scope": db_entitlements.portee_de(r),
           "right_key": r["right_key"], "value": r["value"], "source": r["source"],
           "starts_at": r["starts_at"], "expires_at": r["expires_at"],
           "active": r["active"]}
    if full:
        out.update({k: r[k] for k in _COMPLETS})
    return out


def droits_de_l_org(org_id: int) -> list[dict]:
    """Toutes les lignes posées DANS l'org (org et personnes dans l'org), échues et à
    venir comprises, vue complète — le champ `entitlements` de sa fiche admin."""
    return [_ligne(r, full=True) for r in db_entitlements.lignes_filtrees(org_id=org_id)]


def droits_de_la_personne(sub: str) -> list[dict]:
    """Toutes les lignes posées sur la personne (dans chacune de ses orgs, et partout),
    échues comprises, vue complète — le champ `entitlements` de sa fiche admin. Les
    lignes d'org qui la couvrent n'y sont pas : elles sont sur la fiche de l'org."""
    return [_ligne(r, full=True) for r in db_entitlements.lignes_filtrees(sub=sub)]


# ── la liste transversale ─────────────────────────────────────────────────────

_CURSEUR_ILLISIBLE = ("`cursor` illisible — tronqué, réécrit, ou pris d'une autre "
                      "lecture. Reprends la liste sans `cursor`.")


def _encoder(c: db_entitlements.Curseur) -> str:
    brut = json.dumps(list(c), ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(brut).decode().rstrip("=")


def _decoder(cursor: str) -> db_entitlements.Curseur:
    try:
        clair = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        org, sub, cle, source = json.loads(clair)
    except (binascii.Error, ValueError, TypeError):
        raise AuthzDenied(400, "invalid_cursor", _CURSEUR_ILLISIBLE)
    if not (isinstance(org, int) and all(isinstance(v, str) for v in (sub, cle, source))):
        raise AuthzDenied(400, "invalid_cursor", _CURSEUR_ILLISIBLE)
    return db_entitlements.Curseur(org, sub, cle, source)


def _en_utc(v: Optional[datetime]) -> Optional[datetime]:
    """Une date sans fuseau est lue en UTC — l'horloge de la base, et celle des dates
    rendues."""
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class EntitlementsListInput(BaseModel):
    right: Optional[str] = None
    source: Optional[str] = None
    scope: Optional[PorteeFiltre] = None
    # True = valides maintenant ; False = échues ou à venir ; omis = toutes.
    active: Optional[bool] = None
    expires_before: Optional[datetime] = None
    org_created_after: Optional[datetime] = None
    org_id: Optional[int] = None
    cursor: Optional[str] = None
    limit: int = PAGE
    full: bool = False

    @field_validator("limit")
    @classmethod
    def _borne(cls, v):
        return cap_limit(v, PAGE_MAX)

    @field_validator("expires_before", "org_created_after")
    @classmethod
    def _utc(cls, v):
        return _en_utc(v)


def _lister(ctx: ResolvedCtx, inp: EntitlementsListInput) -> dict:
    if inp.right is not None:
        # Une faute de frappe rendrait une page vide qui se lirait « personne n'a ce
        # droit » : refusée, en nommant la clé.
        try:
            catalogue.droit(inp.right)
        except ValueError as e:
            raise AuthzDenied(400, "entitlement_unknown_key", str(e))
    apres = _decoder(inp.cursor) if inp.cursor else None
    rows = db_entitlements.lignes_filtrees(
        org_id=inp.org_id, right_key=inp.right, source=inp.source, portee=inp.scope,
        actives=inp.active, expires_before=inp.expires_before,
        org_created_after=inp.org_created_after, apres=apres, limite=inp.limit + 1)
    page, suite = rows[:inp.limit], len(rows) > inp.limit
    return {"entitlements": [_ligne(r, full=inp.full) for r in page],
            "count": len(page),
            "next_cursor": _encoder(db_entitlements.curseur_de(page[-1])) if suite else None}


DESCRIPTION_LISTE = (
    "[platform admin] Declared rights (`org_entitlements`) across the platform, without "
    "opening orgs one by one: org rows, rows of a person in an org, rows of a person "
    "everywhere (gifts to a person included). Filters, all optional and combined: "
    "`right` (a catalogue key), `source`, `scope` (org | personne_dans_org | "
    "personne_partout | personne = both person scopes), `active` (true = valid now, "
    "false = expired or not started yet, omitted = all, expired included), "
    "`expires_before` (a deadline set and earlier), `org_created_after` (rows without "
    "an org never match), `org_id`. Paged in a stable order: `limit` (default 100, max "
    "500), `cursor` = the previous page's `next_cursor`. Default rows are compact; "
    "`full=true` adds `org_name`, `org_created_at`, `email`, `granted_by`, "
    "`granted_at`. Read only; reads no subscription, gift or billing table.")

ERREURS_LISTE = (
    DeclaredError(400, "entitlement_unknown_key",
                  "`right` n'est pas une clé du catalogue des droits"),
    DeclaredError(400, "invalid_cursor",
                  "`cursor` illisible, tronqué, ou pris d'une autre lecture"),
)

CAPABILITIES += [
    Capability(
        key="admin.entitlement.list", handler=_lister, Input=EntitlementsListInput,
        Output=EntitlementsPage, authz=PLATFORM_ADMIN, errors=ERREURS_LISTE,
        description=DESCRIPTION_LISTE,
        mcp=None,  # face MCP = la console op-aware `oto_admin_org` (op=entitlements)
        rest=RestBinding("GET", "/api/admin/entitlements"),
    ),
]
