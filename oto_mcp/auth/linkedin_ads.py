"""LinkedIn Ads — a PERSON's connection (LinkedIn 3-legged OAuth, authorization code).

Flow hosted by oto, on the common pattern (`connectors/flow` + `auth/flow`), same shape
as `jefacture`:

1. "Connect" on the card → `_start_flow` returns LinkedIn's consent URL, with a signed,
   single-use `state` carrying the identity;
2. LinkedIn sends the browser back to `/api/linkedin_ads/oauth/callback`
   (`api/oauth_returns.py`); the callback hands the code to the front, which
   completes with its own token (`auth/flow.py`); the code then becomes a grant;
3. the grant goes to the vault, MEMBER tier: the agent reads the ad accounts on which
   THAT person holds a Campaign Manager role.

**Read-only by its scopes** (`r_ads`, `r_ads_reporting` — the lib's `SCOPES`, never
`rw_ads`): the token itself cannot change an ad account.

⚠️ **The application is the platform's**: its `client_id` and `client_secret` (LinkedIn
Developer Portal, app with the Advertising API product) are set by the operator under
the connector `linkedin_ads`: `client_id` at PLATFORM scope of `connector_settings`,
`client_secret` in the vault, encrypted (`auth/app_instance`). Missing, « connect »
refuses NAMING what is missing, and so does a renewal.

**Two shapes of the same row**, said by `meta.token_shape`:

- `refresh` (applications enabled for programmatic refresh tokens): the secret is the
  REFRESH token (365 days, not extended by a refresh); the access token (60 days) lives
  only in a process cache keyed by the hash of the row and its refresh token, renewed
  under a lock on the ROW (a PG advisory lock, across processes AND instances) — the
  `jefacture` pattern, rotation written in the same transaction if LinkedIn ever
  returns a new refresh token;
- `access` (no refresh token issued): the secret is the access token itself, with its
  `access_expires_at`; past it, the row is MARKED and the card says "reconnect".

A dead authorization MARKS the row (`health_ko`), never purges it; a refusal of the
application (bad secret) marks nothing — the person is not at fault.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from .. import credentials_store, status_hints
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link
from . import flow as oauth_flow

logger = logging.getLogger("oto_mcp.auth.linkedin_ads")

CONNECTOR = "linkedin_ads"

_AUD = "linkedin_ads"
CALLBACK_PATH = "/api/linkedin_ads/oauth/callback"

#: `client_id` is a public setting, `client_secret` lives in the vault (`auth/app_instance`).
_REGLAGES = ("client_id", "client_secret")

_COMMANDE = ('oto_admin_connector_setting(op="set", connector="linkedin_ads", '
             'key="<key>", value="<value>")')

_RECONNECTER = "reconnect from your connectors page, connector « LinkedIn Ads »"

SHAPE_REFRESH = "refresh"
SHAPE_ACCESS = "access"

# {opaque key: (access_token, epoch expiry)} — never in the database.
_JETONS: dict[str, tuple[str, float]] = {}
_VERROU = threading.Lock()


class LinkedInAdsReauthRequired(RuntimeError):
    """The person's authorization is dead or expired: they must reconnect."""


class LinkedInAdsRenewalRefused(RuntimeError):
    """LinkedIn refused a renewal for another reason than a dead grant (application
    refused, server error): nothing is marked."""


def _paquet():
    """The lib's `oto.tools.linkedin_ads`, imported AT CALL TIME — the connector stays
    mounted (and refuses, saying so) when the pinned tag does not carry it yet."""
    import importlib

    try:
        return importlib.import_module("oto.tools.linkedin_ads")
    except ImportError as e:
        raise RuntimeError(
            "The `linkedin_ads` connector is not installed on this instance: its core "
            "lives in the connectors library and the pinned tag does not ship it yet. "
            "This is an instance configuration issue, not your account — tell the "
            f"operator. Detail: {e}") from e


def _coeur():
    """`oto.tools.linkedin_ads.auth`: authorize_url, exchange_code, refresh, Grant,
    errors."""
    import importlib

    _paquet()
    return importlib.import_module("oto.tools.linkedin_ads.auth")


# --- the application's coordinates -------------------------------------------

def _reglages() -> dict:
    """Database read: at the start of a flow, on return from a consent, and at the
    RENEWAL of an access token (once per 60 days per person and process)."""
    from . import app_instance

    return app_instance.reglages(CONNECTOR, _REGLAGES)


def coordonnees_manquantes() -> list[str]:
    from . import app_instance

    return app_instance.manquantes(CONNECTOR, _REGLAGES)


def app() -> tuple[str, str]:
    """The instance's `(client_id, client_secret)`, or a refusal that NAMES what is
    missing."""
    poses = _reglages()
    manquantes = [nom for nom in _REGLAGES if not poses.get(nom)]
    if manquantes:
        raise RuntimeError(
            f"The `linkedin_ads` connector is not configured on this instance: missing "
            f"{', '.join(manquantes)}. Nothing to do on your account — the instance "
            f"operator sets them with {_COMMANDE}.")
    return poses["client_id"], poses["client_secret"]


def app_disponible(sub: str) -> bool:
    del sub
    return not coordonnees_manquantes()


# --- the signed state --------------------------------------------------------

def _redirect() -> str:
    return oauth_flow.redirect_uri(CALLBACK_PATH)


def _ctx_org(sub: str) -> int:
    from .. import access  # lazy: avoids any import cycle at boot

    org = access.current_org(sub)
    if org is None:
        raise RuntimeError(
            "No org in context — cannot scope the LinkedIn Ads connection. Sign in "
            "again and retry.")
    return org


def make_state(sub: str, org_id: int, return_app: str = "",
               group_id: Optional[int] = None) -> str:
    return oauth_flow.sign_state(_AUD, {"sub": sub, "org": org_id, "group": group_id,
                                        "app": return_app, "jti": oauth_flow.new_jti()})


def verify_state(state: str) -> Optional[dict]:
    """The state's payload if it is valid, issued FOR this flow, and complete
    (`sub`, `org`, `jti`); `None` otherwise."""
    data = oauth_flow.read_state(_AUD, state)
    if not data:
        return None
    ok = (isinstance(data.get("sub"), str) and isinstance(data.get("org"), int)
          and isinstance(data.get("jti"), str) and data["jti"]
          and (data.get("group") is None or isinstance(data.get("group"), int)))
    if not ok:
        return None
    if not isinstance(data.get("app"), str):
        data["app"] = ""
    return data


def still_allowed(parsed: dict) -> bool:
    """Re-read ON RETURN, not at start: the person is still a member of the org, and
    the connector is still open there. Synchronous (SQL)."""
    from .. import roles
    from ..connectors import activation

    if not roles.is_org_member(parsed["sub"], parsed["org"]):
        return False
    return activation.cran_qui_coupe(CONNECTOR, parsed["org"], parsed.get("group")) is None


# --- starting the flow -------------------------------------------------------

def build_auth_url(sub: str, return_app: str = "") -> str:
    from .. import access  # lazy: avoids any import cycle at boot

    org_id = _ctx_org(sub)
    client_id, _ = app()
    return _coeur().authorize_url(
        client_id, _redirect(),
        make_state(sub, org_id, oauth_flow.resolve_return_app(return_app),
                   access.current_group(sub)))


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    from ..capabilities._types import AuthzDenied

    try:
        return connector_flow.FlowStart(
            auth_url=build_auth_url(ctx.sub, (values or {}).get("app") or ""))
    except RuntimeError as e:  # application not configured, core missing, no org
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


connector_flow.declare(
    CONNECTOR,
    start=_start_flow,
    label="Sign in with LinkedIn",
    callback_path=CALLBACK_PATH,
    app_ready=app_disponible,
)


# --- the vault ---------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: Optional[str]) -> float:
    try:
        return datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _scope(org_id: int, sub: str) -> tuple[str, str]:
    return credentials_store.MEMBER, credentials_store.member_id(org_id, sub)


def _cle(ligne: tuple, secret: str) -> str:
    """Cache key: the row and its secret, hashed (never a clear-text secret as a key)."""
    return hashlib.sha256("|".join((*ligne, secret)).encode()).hexdigest()


def _garder(ligne: tuple, secret: str, access_token: str, expires_at: float) -> None:
    with _VERROU:
        _JETONS[_cle(ligne, secret)] = (access_token, expires_at)


def _en_cache(ligne: tuple, secret: str) -> Optional[str]:
    with _VERROU:
        cached = _JETONS.get(_cle(ligne, secret))
    return cached[0] if cached and cached[1] > time.time() + 60 else None


@contextmanager
def _verrou_ligne(ligne: tuple) -> Iterator[object]:
    """A transaction holding the renewal lock of ONE vault row — an advisory lock in
    the shared base, so it holds across processes and across instances. Yields the
    connection, for a rotation to be written under the lock."""
    from ..db import _connect

    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     ("linkedin_ads-refresh|" + "|".join(ligne),))
        yield conn


def _qui(access_token: str) -> str:
    """The person's URN (`urn:li:person:…`), read from their ad account roles —
    BEST-EFFORT: no role yet, or a refusal, never blocks storing the grant."""
    try:
        roles = _paquet().LinkedInAdsClient(access_token).list_account_roles()
        for el in (roles or {}).get("elements") or []:
            if isinstance(el, dict) and el.get("user"):
                return str(el["user"])
    except Exception as e:  # noqa: BLE001 — identity is a nicety, not a condition
        logger.info("linkedin_ads: identity not read at connection (%s)",
                    type(e).__name__)
    return ""


def exchange_and_persist(parsed: dict, code: str) -> dict:
    """The code brought back by the browser → a grant → the vault. The secret is the
    refresh token when LinkedIn issues one, the access token otherwise
    (`meta.token_shape`); a reconnection replaces the row — under the row's renewal
    lock — and clears its health mark."""
    sub, org_id = parsed["sub"], parsed["org"]
    client_id, client_secret = app()
    grant = _coeur().exchange_code(client_id, client_secret, code, _redirect())
    maintenant = datetime.now(timezone.utc)
    acces_fin = maintenant + timedelta(seconds=int(grant.expires_in))
    meta = {"scopes": grant.scope, "connected_at": _iso(maintenant),
            "access_expires_at": _iso(acces_fin)}
    if grant.refresh_token:
        meta["token_shape"] = SHAPE_REFRESH
        if grant.refresh_token_expires_in:
            meta["refresh_expires_at"] = _iso(
                maintenant + timedelta(seconds=int(grant.refresh_token_expires_in)))
        secret = grant.refresh_token
    else:
        meta["token_shape"] = SHAPE_ACCESS
        secret = grant.access_token
    if (member := _qui(grant.access_token)):
        meta["member_urn"] = member
    entity_type, entity_id = _scope(org_id, sub)
    ligne = (entity_type, entity_id, "")
    with _verrou_ligne(ligne) as conn:
        credentials_store.set_credential(entity_type, entity_id, CONNECTOR, secret,
                                         set_by=sub, meta=meta, conn=conn)
    _garder(ligne, secret, grant.access_token, acces_fin.timestamp())
    logger.info("linkedin_ads: account connected (org=%s, shape=%s)", org_id,
                meta["token_shape"])
    return {"connected": True, "token_shape": meta["token_shape"]}


def _lire(ligne: tuple) -> dict:
    entity_type, entity_id, account = ligne
    row = credentials_store.get_credential_with_meta(entity_type, entity_id, CONNECTOR,
                                                     account=account)
    if not row:
        raise LinkedInAdsReauthRequired(
            f"No LinkedIn Ads connection any more for this account: {_RECONNECTER}.")
    return row


def _mort(ligne: tuple, message: str, cause: Optional[BaseException] = None):
    connector_health.mark_rejected(ligne[0], ligne[1], CONNECTOR, ligne[2], message)
    raise LinkedInAdsReauthRequired(message) from cause


def _renouveler(ligne: tuple, secret: str, *, reprise: bool = False) -> str:
    """Under the row's lock: re-read, reuse a token a concurrent caller just got,
    otherwise renew (shape `refresh`) or check the expiry (shape `access`)."""
    coeur = _coeur()
    with _verrou_ligne(ligne) as conn:
        row = _lire(ligne)
        secret = row["secret"]
        meta = row.get("meta") or {}
        if (deja := _en_cache(ligne, secret)):
            return deja  # renewed while we waited for the lock
        if meta.get("token_shape") == SHAPE_ACCESS:
            fin = _epoch(meta.get("access_expires_at"))
            if fin > time.time() + 60:
                _garder(ligne, secret, secret, fin)
                return secret
            refus: Optional[BaseException] = None
            expire = True
        else:
            expire = False
            client_id, client_secret = app()
            try:
                grant = coeur.refresh(client_id, client_secret, secret)
            except coeur.LinkedInAdsGrantExpired as e:
                refus = e
            except coeur.LinkedInAdsAuthError as e:
                # The application is refused, or the server failed: the person's grant
                # is not at fault — nothing is marked.
                raise LinkedInAdsRenewalRefused(
                    f"{e} — the access could not be renewed; if it persists, tell the "
                    "instance operator (application coordinates).") from e
            else:
                if grant.refresh_token and grant.refresh_token != secret:
                    credentials_store.set_credential(
                        ligne[0], ligne[1], CONNECTOR, grant.refresh_token,
                        set_by=row.get("set_by"), meta=meta, account=ligne[2],
                        conn=conn)
                    secret = grant.refresh_token
                _garder(ligne, secret, grant.access_token,
                        time.time() + int(grant.expires_in))
                refus = None
    if expire:
        # A reconnection between our read and the lock replaced the secret: retry once.
        if not reprise and _lire(ligne)["secret"] != secret:
            return _renouveler(ligne, secret, reprise=True)
        _mort(ligne, "The LinkedIn sign-in has expired (LinkedIn issues 60-day access "
                     f"tokens and this application gets no refresh token): {_RECONNECTER}.")
    if refus is None:
        connector_health.record_health(CONNECTOR, ligne, True, None)
        return grant.access_token
    # A refusal is checked against the row before it marks anything: a secret that
    # changed meanwhile is a reconnection, not a dead grant.
    if not reprise and _lire(ligne)["secret"] != secret:
        return _renouveler(ligne, secret, reprise=True)
    _mort(ligne, "LinkedIn no longer accepts this sign-in (expired or revoked): "
                 f"{_RECONNECTER}.", refus)


def _ligne_de(sub: str):
    from .. import access  # lazy: avoids any import cycle at boot

    rc = access.resolve_credential(CONNECTOR, want="byo", sub=sub)
    return (rc.entity_type, rc.entity_id, rc.account or ""), rc.key


def access_token_for(sub: str) -> str:
    """A valid access token for the person. Raises a `McpError` (not connected),
    `RuntimeError` (core missing, application not configured),
    `LinkedInAdsRenewalRefused` (application refused) or `LinkedInAdsReauthRequired`
    (dead or expired authorization: the row is marked, the card says "reconnect")."""
    ligne, secret = _ligne_de(sub)
    return _en_cache(ligne, secret) or _renouveler(ligne, secret)


def oublier_jeton(sub: str) -> None:
    """Drops the person's cached access token after LinkedIn refused it (401): the
    next call renews instead of replaying it until its announced expiry."""
    ligne, secret = _ligne_de(sub)
    with _VERROU:
        _JETONS.pop(_cle(ligne, secret), None)


def marquer_rejet(sub: str, message: str) -> None:
    """A 401 that a renewed token does not cure (shape `access`, or a token LinkedIn
    revoked): the row is marked so that the card says "reconnect". Never raises."""
    try:
        ligne, _ = _ligne_de(sub)
        connector_health.mark_rejected(ligne[0], ligne[1], CONNECTOR, ligne[2], message)
    except Exception:  # noqa: BLE001 — a failed marking does not mask the refusal
        logger.warning("linkedin_ads: health marking failed", exc_info=True)


# --- what the card displays --------------------------------------------------

def _row(org_id: int, sub: str) -> Optional[dict]:
    """The person's row, WITHOUT its secret, read in the member's inventory
    (`list_credentials`): the very read the presence probe of `status_for` makes,
    memoized per call — the card costs no query of its own on the connector list."""
    entity_type, entity_id = _scope(org_id, sub)
    return next((r for r in credentials_store.list_credentials(entity_type, entity_id)
                 if r["connector"] == CONNECTOR and r["account"] == ""), None)


def _link_state(sub: str) -> connector_link.LinkState:
    from .. import access  # lazy

    org = access.current_org(sub)
    row = _row(org, sub) if org is not None else None
    if not row:
        return connector_link.LinkState(linked=False)
    meta = row.get("meta") or {}
    return connector_link.LinkState(
        linked=True, accounts=1, set_at=str(row.get("set_at") or "") or None,
        health_ko=True if meta.get("health_ko") else None,
        health_reason=meta.get("health_reason") or None)


connector_link.register(CONNECTOR, _link_state)


def _etape_manquante(sub: str, org, group, entry: dict) -> Optional[str]:
    """What remains to be done — and by WHOM. Without the application's coordinates,
    it is not for the person to click "Connect" in a loop."""
    del org, group, entry
    if coordonnees_manquantes():
        return "LinkedIn app to be configured by the operator"
    etat = _link_state(sub)
    if not etat.linked:
        return "Sign in with LinkedIn"
    if etat.health_ko:
        return "Sign-in expired — reconnect"
    return None


status_hints.register(CONNECTOR, _etape_manquante)


def avertir_au_demarrage() -> None:
    """Says AT BOOT what will prevent the connector from serving. Never raises."""
    try:
        manquantes = coordonnees_manquantes()
    except Exception as e:  # noqa: SILENT — at boot the database may not be ready
        logger.info("linkedin_ads: configuration not verifiable at startup (%s) — "
                    "the first flow will decide.", type(e).__name__)
        return
    if manquantes:
        logger.warning(
            "linkedin_ads: connector mounted but NOT configured — %s missing. "
            "The \"Connect\" button will refuse, saying so. Set with: %s",
            ", ".join(manquantes), _COMMANDE)


# --- the return: the callback hands over, the front completes (`auth/flow.py`) ------

#: LinkedIn's own words for the person's refusal (its documented `error` values).
_REFUS_LINKEDIN = {"user_cancelled_login": oauth_flow.REFUS_FOURNISSEUR,
                   "user_cancelled_authorize": oauth_flow.REFUS_FOURNISSEUR}


def _refusal(query) -> str:
    erreur = query.get("error")
    return oauth_flow.etat_sans_code(_REFUS_LINKEDIN.get(erreur, erreur))


def _return_state(state: Optional[str]) -> Optional[oauth_flow.ReturnState]:
    d = verify_state(state) if state else None
    if d is None:
        return None
    return oauth_flow.ReturnState(d["sub"], d["org"],
                                  oauth_flow.resolve_return_app(d["app"]), CONNECTOR,
                                  d["jti"], d)


def _finish_return(d: dict, code: str) -> dict:
    """Re-read ON RETURN (`still_allowed`): still a member of the org, connector still
    open there; then the exchange and the vault."""
    if not still_allowed(d):
        raise oauth_flow.not_allowed(
            "You are no longer a member of this organization, or LinkedIn Ads is now "
            "turned off there.")
    exchange_and_persist(d, code)
    return {"account": None, "scope": "member"}


RETURN = oauth_flow.OAuthReturn(
    audience=_AUD, callback_path=CALLBACK_PATH, label="LinkedIn Ads", default_card=CONNECTOR,
    read=_return_state, finish=_finish_return, cards=lambda: (CONNECTOR,), refusal=_refusal)
oauth_flow.declare_return(RETURN)
