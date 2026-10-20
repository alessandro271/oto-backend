"""Admin capability: what ANTHROPIC counted on the instance's platform keys, against
what the units budget settled (ADR 0009) — the check that does not trust the metering.

A platform `anthropic` key served to granted orgs meters itself: each model call is
reserved then settled in the units budget, in tokens (`runner_jobs._regler_un_tour`,
`credit_movements.meta.tokens`, `key_mode='platform'`). That is our code reporting on
itself. This reads Anthropic's own usage report for one UTC day and compares it, unit
by unit (`credits_tours.UNITES_DU_MODELE`), with what the budget settled — in total and
PER MODEL (`by_model`), since each model has its own lines in the scale
(`messages:<model>`, the id without its snapshot date on both sides). Tokens only: no
tariff and no money here — pricing is the commerce's.

## What it catches

- **a gap on the platform keys** — tokens Anthropic counted that no settlement
  carries: a worker that died mid-call, a metering bug. Settled ABOVE Anthropic's
  count is a gap too (double settlement, a key mix-up).
- **tokens on any OTHER key** of the Anthropic organization (`other_keys`) — a worker
  running on its environment key is not metered at all. `api_key_id: null` is usage
  made in the Console.

## What it does NOT do

- **Say which org owes a gap.** Anthropic's report knows API keys, not our orgs.
- **Read the TENANT rung.** A tenant key may sit in another Anthropic organization;
  only settlements under `key_mode='platform'` are compared.
- **Bill, alert or remember anything.** It returns a verdict; whoever charges the
  orgs decides what is worth an alert.

## Which Anthropic key is the platform key

Matched, not configured: each platform row's secret is compared with the
`partial_key_hint` of the organization's keys (`anthropic_usage.indice_correspond`),
so a rotated key is followed without a setting to update. A row no hint designates is
listed in `unmatched_platform_rows`; a row two hints designate is `ambiguous` and
counted under neither. Secrets are read and dropped in the worker thread: the output
carries key ids and names only.

Requires `OTO_ANTHROPIC_ADMIN_KEY` (an Anthropic Admin API key); absent, a named
refusal (`admin_key_not_configured`). `PLATFORM_ADMIN`, read-only.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .. import anthropic_usage, credentials_store
from ..access import credits_tours as tours
from ..db import credits as db_credits
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

FOURNISSEUR = "anthropic"


class ModelSpendInput(BaseModel):
    day: Optional[date] = Field(None, description=(
        "The UTC day to reconcile (YYYY-MM-DD). Omitted = yesterday — Anthropic's report "
        "is final a few minutes after the day ends."))


class Tokens(BaseModel):
    """Tokens under the units of a model call (`access/credits_tours.UNITES_DU_MODELE`)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class PlatformKeySpend(BaseModel):
    label: str
    api_key_id: Optional[str] = None
    name: Optional[str] = None
    anthropic_tokens: Tokens = Tokens()
    # `matched` | `unmatched` (no hint designates this row) | `ambiguous` (several do)
    match: str


class OtherKeySpend(BaseModel):
    api_key_id: Optional[str] = None   # null = usage made in the Console
    name: Optional[str] = None
    tokens: Tokens


class ModelTokens(BaseModel):
    """One model (`credits_tours.cle_du_modele`; null = Anthropic named none)."""
    model: Optional[str] = None
    anthropic_tokens: Tokens
    settled_tokens: Tokens
    gap_tokens: Tokens


class ModelSpend(BaseModel):
    day: str
    platform_keys: list[PlatformKeySpend]
    anthropic_tokens: Tokens
    settled_tokens: Tokens
    # Anthropic minus settled, per unit: positive = tokens no settlement carries.
    gap_tokens: Tokens
    by_model: list[ModelTokens]
    other_keys: list[OtherKeySpend]
    unmatched_platform_rows: list[str]


def _cles_plateforme() -> list[tuple[str, str]]:
    """(label, secret) de chaque clé `anthropic` posée au rang plateforme."""
    out = []
    for r in credentials_store.list_platform_credentials(FOURNISSEUR):
        ligne = credentials_store.get_credential_with_meta(
            credentials_store.PLATFORM, r["label"], FOURNISSEUR, "")
        if ligne and ligne.get("secret"):
            out.append((r["label"], ligne["secret"]))
    return out


def _somme(a: dict, b: dict) -> dict:
    return {u: a.get(u, 0) + b.get(u, 0) for u in tours.UNITES_DU_MODELE}


def _ecart(a: dict, b: dict) -> dict:
    return {u: a[u] - b[u] for u in tours.UNITES_DU_MODELE}


def rapprocher(jour: date, *, usage: list[dict], cles: list[dict],
               plateforme: list[tuple[str, str]], regle: dict) -> dict:
    """Le rapprochement d'un jour, en jetons — pur, chaque entrée injectable. `regle` :
    les jetons réglés par fonction du barème (`messages:<modèle>`)."""
    noms = {c.get("id"): c.get("name") for c in cles}
    par_ligne, ids_plateforme = [], set()
    for label, secret in plateforme:
        trouvees = [c for c in cles if anthropic_usage.indice_correspond(
            c.get("partial_key_hint"), secret)]
        if len(trouvees) == 1:
            ids_plateforme.add(trouvees[0]["id"])
            par_ligne.append({"label": label, "api_key_id": trouvees[0]["id"],
                              "name": trouvees[0].get("name"), "match": "matched"})
        else:
            par_ligne.append({"label": label, "match":
                              "ambiguous" if trouvees else "unmatched"})
    plat: dict[str, dict] = {}
    autres: dict[Optional[str], dict] = {}
    par_modele: dict[Optional[str], dict] = {}
    for res in usage:
        cle, t = res.get("api_key_id"), anthropic_usage.jetons(res)
        cible = plat if cle in ids_plateforme else autres
        cible[cle] = _somme(cible.get(cle, {}), t)
        if cle in ids_plateforme:
            m = tours.cle_du_modele(res["model"]) if res.get("model") else None
            par_modele[m] = _somme(par_modele.get(m, {}), t)
    zero = {u: 0 for u in tours.UNITES_DU_MODELE}
    regle_par_modele = {f[len(tours.PREFIXE_DES_MODELES):]: _somme(zero, t)
                        for f, t in regle.items()}
    for ligne in par_ligne:
        ligne["anthropic_tokens"] = plat.get(ligne.get("api_key_id"), zero)
    anthropic = zero
    for t in plat.values():
        anthropic = _somme(anthropic, t)
    total = zero
    for t in regle_par_modele.values():
        total = _somme(total, t)
    modeles = sorted(set(par_modele) | set(regle_par_modele), key=lambda m: m or "")
    return {
        "day": jour.isoformat(), "platform_keys": par_ligne,
        "anthropic_tokens": anthropic, "settled_tokens": total,
        "gap_tokens": _ecart(anthropic, total),
        "by_model": [{"model": m, "anthropic_tokens": par_modele.get(m, zero),
                      "settled_tokens": regle_par_modele.get(m, zero),
                      "gap_tokens": _ecart(par_modele.get(m, zero),
                                           regle_par_modele.get(m, zero))}
                     for m in modeles],
        "other_keys": [{"api_key_id": k, "name": noms.get(k), "tokens": t}
                       for k, t in sorted(autres.items(), key=lambda kv: -sum(kv[1].values()))
                       if sum(t.values()) > 0],
        "unmatched_platform_rows": [l["label"] for l in par_ligne if l["match"] != "matched"],
    }


def _lire(jour: date, key: str) -> dict:
    """Tout ce qui bloque — réseau, coffre, base — dans UN fil hors de la boucle."""
    return rapprocher(jour, usage=anthropic_usage.usage_du_jour(jour, key),
                      cles=anthropic_usage.cles_d_api(key), plateforme=_cles_plateforme(),
                      regle=db_credits.jetons_du_jour(
                          tours.MODELE_CONNECTOR, tours.PREFIXE_DES_MODELES,
                          credentials_store.PLATFORM, jour, tours.UNITES_DU_MODELE))


async def _model_spend(ctx: ResolvedCtx, inp: ModelSpendInput) -> dict:
    key = anthropic_usage.admin_key()
    if not key:
        raise AuthzDenied(409, "admin_key_not_configured",
                          "OTO_ANTHROPIC_ADMIN_KEY is not set on this instance: without an "
                          "Anthropic Admin API key, its usage report cannot be read.")
    jour = inp.day or (datetime.now(timezone.utc) - timedelta(days=1)).date()
    try:
        return await asyncio.to_thread(_lire, jour, key)
    except anthropic_usage.AnthropicAdminError as e:
        logger.warning("model spend %s : %s", jour, e)
        raise AuthzDenied(502, "anthropic_admin_unavailable",
                          f"Anthropic's Admin API did not answer: {e}") from None


CAPABILITIES += [
    Capability(
        key="admin.model_spend", handler=_model_spend,
        Input=ModelSpendInput, Output=ModelSpend,
        authz=PLATFORM_ADMIN,
        errors=(
            DeclaredError(409, "admin_key_not_configured",
                          "the instance has no `OTO_ANTHROPIC_ADMIN_KEY`"),
            DeclaredError(502, "anthropic_admin_unavailable",
                          "Anthropic's Admin API refused or did not answer"),
        ),
        description=(
            "[platform admin] Reconciles one UTC day (default: yesterday), in TOKENS, "
            "of what ANTHROPIC counted on this instance's platform `anthropic` keys — "
            "its own usage report — against what the units budget settled for model "
            "calls on the platform rung (`anthropic.messages:<model>`). Per unit (input, "
            "output, cache write, cache read): per platform key its Anthropic tokens, the "
            "totals, `gap_tokens` (positive = tokens no settlement carries), `by_model` "
            "(the same per model, snapshot date dropped), "
            "`other_keys` (tokens on any other key of the Anthropic organization: "
            "unmetered by construction; null id = Console) and "
            "`unmatched_platform_rows`. No tariff, no money. Platform keys are matched "
            "to Anthropic's by `partial_key_hint`. Cannot attribute a gap to an org; "
            "tenant keys are out of scope. Read-only."),
        mcp="oto_admin_model_spend",
        rest=RestBinding("GET", "/api/admin/platform-keys/model-spend"),
    ),
]
