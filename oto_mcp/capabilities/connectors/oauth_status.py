"""Capacités génériques « lire l'état / déconnecter » d'un consentement OAuth —
un chemin fixe qui ne nomme pas le connecteur, symétrique de `me.connector_connect`
(`connect.py`). Ferme les items 2/3 d'oto-dashboard#125 : le widget du dashboard
construisait `/api/${name}/oauth/status` et `DELETE /api/${name}/oauth`, les deux
derniers endroits où un nom de connecteur voyageait dans une URL.

**Portée : google, câblé ICI.** ⚠️ Ils étaient TROIS jusqu'au 2026-09-09 — atlassian
et folkmcp sont partis avec la fédération MCP (ADR 0069). Un connecteur hors de cette
liste répond `400 no_oauth_status` : ce n'est pas une garde défensive gratuite, c'est
le même principe que `connector_flow.supports()` — un geste qui n'est pas déclaré
n'est pas mimé en silence.

⚠️ **Le chemin est resté GÉNÉRIQUE alors qu'il ne sert plus qu'un connecteur**, et
c'est délibéré : `/api/me/connectors/{name}/…` est le contrat que le dashboard appelle,
il ne se replie pas sur `/api/google/…` parce que la liste a rétréci. Le jour où un
second connecteur OAuth veut ces verbes, il lui suffit d'un `declare_status`.

**Contrainte 1 (bloquante, arbitrage du 04/09/2026) — `me.connector_status` ne crée pas
une seconde vérité.** Son état est dérivé d'`access.status_for(sub)`, la MÊME source
que `/api/me` (cf. `capabilities/me_account.py::_me`) — jamais un appel parallèle à
`google_oauth.list_accounts` qui pourrait diverger. C'est pour ça que ce fichier
n'importe AUCUN module `auth.*` dans le chemin de lecture (`_status`) : seul
`_disconnect` (et le wrapper ci-dessous) l'importe, paresseusement, à l'appel.

⚠️ **google est multi-compte, et `access.status_for` ne porte qu'UNE identité par
défaut** (`ProviderStatus.identity_id`/`identity_label`, singulier — héritage du temps
où Google était mono-compte). Le contrat commun ci-dessous (`connected`, `set_at`,
`health_ko`, `health_reason`) ne porte donc RIEN de spécifique à google au-delà : forcer
un champ `accounts` ici irait le chercher dans une autre lecture
(`google_oauth.list_accounts`), soit exactement la seconde vérité que la contrainte 1
interdit. La richesse multi-compte continue de passer par `connectors.identities`
(op=list) — hors de ce lot (option A explicitement écartée, cf. oto-dashboard#125).

**Contrainte 2 (bloquante, décision d'Alexis) — `me.connector_disconnect` est
irréversible, sans double étape.** Un seul appel : révoque chez le fournisseur quand le
mécanisme le permet, et DANS TOUS LES CAS retire (ou marque) la ligne locale. Le contrat
de sortie est `FederationDisconnected` (`ok`, `disconnected`), réutilisé tel quel.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import access
from ...connectors import flow_status
from .._authz import SUB_ONLY
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..federated_oauth import FederationDisconnected
from ..registry import CAPABILITIES


# --- Entrée / sortie ---------------------------------------------------------

class ConnectorOAuthStatusInput(BaseModel):
    name: str                                    # connecteur, depuis le chemin


class ConnectorOAuthDisconnectInput(BaseModel):
    name: str                                    # connecteur, depuis le chemin


class ConnectorOAuthStatus(BaseModel):
    """État d'un consentement OAuth — dérivé d'`access.status_for` (contrainte 1).
    `connected: false` avec `set_at: null` est l'état normal d'un compte jamais
    connecté.

    `health_ko`/`health_reason` (oto#25 lot a) sont `None` tant que rien n'a été
    constaté — jamais `False` : ce contrat ne sait pas confirmer une santé bonne,
    seulement en rapporter le rejet, une fois écrit (même politique que
    `connector_link.LinkState`)."""
    connected: bool
    set_at: Optional[str] = None
    health_ko: Optional[bool] = None
    health_reason: Optional[str] = None


def _require_oauth(name: str) -> None:
    """Un connecteur qui n'a pas déclaré ses verbes ici n'est pas mimé en silence —
    même principe que `connector_flow.supports()`. Aujourd'hui : google, et personne
    d'autre (atlassian et folkmcp sont partis avec la fédération, ADR 0069)."""
    if not flow_status.supports(name):
        raise AuthzDenied(
            400, "no_oauth_status",
            f"« {name} » n'a pas d'état OAuth générique : ce n'est pas un des "
            "connecteurs couverts (google). Son credential se lit par "
            "`connectors.me` comme les autres.")


def _status(ctx: ResolvedCtx, inp: ConnectorOAuthStatusInput) -> dict:
    _require_oauth(inp.name)
    # SOURCE UNIQUE (contrainte 1) : la MÊME lecture que `/api/me`
    # (`capabilities/me_account.py::_me`), jamais un second appel à `auth.*`.
    snapshot = access.status_for(ctx.sub)
    entry = (snapshot.get("providers") or {}).get(inp.name) or {}
    return {
        "connected": bool(entry.get("user_key_configured")),
        "set_at": entry.get("session_set_at"),
        "health_ko": entry.get("health_ko"),
        "health_reason": entry.get("health_reason"),
    }


async def _disconnect(ctx: ResolvedCtx, inp: ConnectorOAuthDisconnectInput) -> dict:
    _require_oauth(inp.name)
    return await flow_status.disconnect(inp.name, ctx)


# --- Câblage (déclaration IMPORT-TIME, comme `connector_flow.declare` — cf.
# `flow_status.py`). L'import d'`auth.*` reste À L'APPEL (dans le wrapper), pas ici :
# ce module monte des clients HTTP et lit sa config au chargement. ----------------

def _google_disconnect(ctx: ResolvedCtx) -> dict:
    from ...auth import google as google_oauth
    # `account=None` = TOUS les comptes du sub, même comportement que
    # `federated_oauth._google_revoke` sans paramètre — le geste générique n'a pas de
    # notion de compte nommé (celle-là reste `connectors.identities`, hors de ce lot).
    google_oauth.revoke(ctx.sub, account=None)
    return {"ok": True, "disconnected": True}


flow_status.declare_status("google", disconnect=_google_disconnect)


CAPABILITIES += [
    Capability(
        key="me.connector_status",
        handler=_status,
        Input=ConnectorOAuthStatusInput,
        authz=SUB_ONLY,
        Output=ConnectorOAuthStatus,
        mcp=None,     # geste de lecture d'écran (dashboard) ; pas de pendant agent utile
        errors=(DeclaredError(400, "no_oauth_status",
                              "ce connecteur n'a pas d'état OAuth générique "
                              "(hors google)"),),
        rest=RestBinding("GET", "/api/me/connectors/{name}/oauth-status"),
        description=("Mon consentement OAuth pour ce connecteur (google) est-il posé, "
                     "et depuis quand — dérivé de la même source que `/api/me`. "
                     "`connected: false` avec `set_at: null` est l'état normal d'un "
                     "compte jamais connecté."),
    ),
    Capability(
        key="me.connector_disconnect",
        handler=_disconnect,
        Input=ConnectorOAuthDisconnectInput,
        authz=SUB_ONLY,
        Output=FederationDisconnected,
        mcp=None,
        errors=(DeclaredError(400, "no_oauth_status",
                              "ce connecteur n'a pas d'état OAuth générique "
                              "(hors google)"),),
        rest=RestBinding("DELETE", "/api/me/connectors/{name}/oauth"),
        description=("Révoque mon consentement OAuth pour ce connecteur (google) — "
                     "chez le fournisseur quand le mécanisme le "
                     "permet, et dans tous les cas retire la ligne locale. UN SEUL "
                     "appel, irréversible : jamais d'état intermédiaire en attente de "
                     "confirmation. Idempotent : `disconnected: false` veut dire qu'il "
                     "n'y avait rien à retirer, pas que le retrait a échoué."),
    ),
]
