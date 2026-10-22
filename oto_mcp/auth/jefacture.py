"""jefacture.com — a PERSON's connection (OAuth 2.0 authorization code, Banqup
platform, French region).

Flow hosted by oto, on the common pattern (`connectors/flow` + `auth/flow`), same
shape as `ubersuggest`, with the application of `meta_ads`:

1. "Connect" on the card → `_start_flow` returns the jefacture.com sign-in URL
   (a QR code scanned with the jefacture.com mobile app, then the consent on first
   sign-in), with a signed, single-use `state` carrying the identity;
2. jefacture.com sends the browser back to `/api/jefacture/oauth/callback`
   (`api/jefacture.py`); the code there becomes a refresh token;
3. the refresh token goes to the vault, MEMBER tier, with the account's address
   (`get_me().email`) in `meta`: the agent reads what THAT person reaches.

⚠️ **The application is the platform's**: its `client_id` (the API key of the
application on the Banqup developer portal) and `client_secret` live at PLATFORM
scope of `connector_settings`, connector `jefacture`, set by the operator. Missing,
« connect » refuses NAMING what is missing, and so does a renewal.

**Renewal.** `access_token_for` asks for a fresh access token with the refresh
token when the cached one expires within a minute. The platform ROTATES the refresh
token: renewal runs under a lock on the ROW (an advisory lock in the shared base,
so across processes AND instances), re-reads the row once the lock is held, and
writes the rotation in the same transaction — a second caller finds the renewed
token instead of replaying a spent one. A dead grant is re-checked against the row
before it marks anything (a secret that changed meanwhile is a reconnection, retried
once). The access token never goes to the database: a process cache keyed by the
hash of the row and its refresh token. A dead authorization MARKS the row
(`health_ko`), never purges it; a refusal of the application (bad secret) marks
nothing — the person is not at fault.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from .. import credentials_store, status_hints
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link
from . import flow as oauth_flow

logger = logging.getLogger("oto_mcp.auth.jefacture")

CONNECTOR = "jefacture"

_AUD = "jefacture"
CALLBACK_PATH = "/api/jefacture/oauth/callback"

#: `client_secret` carries the `_secret` suffix that the admin console masks.
_REGLAGES = ("client_id", "client_secret")

_COMMANDE = ('oto_admin_connector_setting(op="set", connector="jefacture", '
             'key="<key>", value="<value>")')

_RECONNECTER = "reconnect from your connectors page, connector « jefacture.com »"

# {opaque key: (access_token, epoch expiry)} — never in the database.
_JETONS: dict[str, tuple[str, float]] = {}
_VERROU = threading.Lock()


class JefactureReauthRequired(RuntimeError):
    """The person's authorization is dead: they must reconnect."""


class JefactureRenewalRefused(RuntimeError):
    """The authorization server refused a renewal for another reason than a dead
    grant (application refused, server error): nothing is marked."""


def _paquet():
    """The lib's `oto.tools.jefacture`, imported AT CALL TIME — the connector stays
    mounted (and refuses, saying so) when the pinned tag does not carry it yet."""
    import importlib

    try:
        return importlib.import_module("oto.tools.jefacture")
    except ImportError as e:
        raise RuntimeError(
            "The `jefacture` connector is not installed on this instance: its core "
            "lives in the connectors library and the pinned tag does not ship it yet. "
            "This is an instance configuration issue, not your account — tell the "
            f"operator. Detail: {e}") from e


def _coeur():
    """`oto.tools.jefacture.auth`: authorize_url, exchange_code, refresh, Grant, errors."""
    import importlib

    _paquet()
    return importlib.import_module("oto.tools.jefacture.auth")


# --- the application's coordinates -------------------------------------------

def _reglages() -> dict:
    """Database read: at the start of a flow, on return from a consent, and at the
    RENEWAL of an access token (once per hour per person)."""
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == CONNECTOR
            and r["key"] in _REGLAGES}


def coordonnees_manquantes() -> list[str]:
    poses = _reglages()
    return [nom for nom in _REGLAGES if not poses.get(nom)]


def app() -> tuple[str, str]:
    """The instance's `(client_id, client_secret)`, or a refusal that NAMES what is
    missing."""
    poses = _reglages()
    manquantes = [nom for nom in _REGLAGES if not poses.get(nom)]
    if manquantes:
        raise RuntimeError(
            f"The `jefacture` connector is not configured on this instance: missing "
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
            "No org in context — cannot scope the jefacture.com connection. Sign in "
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
    label="Sign in with jefacture.com",
    callback_path=CALLBACK_PATH,
    app_ready=app_disponible,
)


# --- the vault ---------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope(org_id: int, sub: str) -> tuple[str, str]:
    return credentials_store.MEMBER, credentials_store.member_id(org_id, sub)


def _cle(ligne: tuple, refresh_token: str) -> str:
    """Cache key: the row and its refresh token, hashed (never a clear-text secret
    as a key)."""
    return hashlib.sha256("|".join((*ligne, refresh_token)).encode()).hexdigest()


def _garder(ligne: tuple, grant) -> None:
    with _VERROU:
        _JETONS[_cle(ligne, grant.refresh_token)] = (
            grant.access_token, time.time() + int(grant.expires_in))


def _en_cache(ligne: tuple, refresh_token: str) -> Optional[str]:
    with _VERROU:
        cached = _JETONS.get(_cle(ligne, refresh_token))
    return cached[0] if cached and cached[1] > time.time() + 60 else None


@contextmanager
def _verrou_ligne(ligne: tuple) -> Iterator[object]:
    """A transaction holding the renewal lock of ONE vault row — an advisory lock in
    the shared base, so it holds across processes and across instances. Yields the
    connection, for the rotation to be written under the lock."""
    from ..db import _connect

    with _connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     ("jefacture-refresh|" + "|".join(ligne),))
        yield conn


def exchange_and_persist(parsed: dict, code: str) -> dict:
    """The code brought back by the browser → a grant → the vault. The refresh token
    is the secret (`secret_kind="oauth"`), the account's address goes to `meta`; a
    reconnection replaces the row — under the row's renewal lock — and clears its
    health mark."""
    sub, org_id = parsed["sub"], parsed["org"]
    client_id, client_secret = app()
    grant = _coeur().exchange_code(client_id, client_secret, code, _redirect())
    if not grant.refresh_token:
        raise RuntimeError("jefacture.com did not issue a refresh token: nothing was saved.")
    moi = _paquet().JefactureClient(access_token=grant.access_token).get_me()
    email = (moi or {}).get("email") if isinstance(moi, dict) else None
    if not email:
        raise RuntimeError("jefacture.com did not say which account signed in "
                           "(`email` missing from the identity): nothing was saved.")
    entity_type, entity_id = _scope(org_id, sub)
    meta = {"email": email, "scopes": grant.scope,
            "connected_at": _iso(datetime.now(timezone.utc))}
    ligne = (entity_type, entity_id, "")
    with _verrou_ligne(ligne) as conn:
        credentials_store.set_credential(entity_type, entity_id, CONNECTOR,
                                         grant.refresh_token, set_by=sub, meta=meta,
                                         conn=conn)
    _garder(ligne, grant)
    logger.info("jefacture: account connected (org=%s)", org_id)
    return {"connected": True}


def _lire(ligne: tuple) -> dict:
    entity_type, entity_id, account = ligne
    row = credentials_store.get_credential_with_meta(entity_type, entity_id, CONNECTOR,
                                                     account=account)
    if not row:
        raise JefactureReauthRequired(
            f"No jefacture.com connection any more for this account: {_RECONNECTER}.")
    return row


def _renouveler(ligne: tuple, refresh_token: str, *, reprise: bool = False) -> str:
    """Under the row's lock: re-read, reuse a token a concurrent caller just got,
    otherwise renew and write the rotation in the same transaction."""
    coeur = _coeur()
    with _verrou_ligne(ligne) as conn:
        row = _lire(ligne)
        refresh_token = row["secret"]
        if (deja := _en_cache(ligne, refresh_token)):
            return deja  # renewed while we waited for the lock
        client_id, client_secret = app()
        try:
            grant = coeur.refresh(client_id, client_secret, refresh_token)
        except coeur.JefactureGrantExpired as e:
            refus = e
        except coeur.JefactureAuthError as e:
            # The application is refused, or the server failed: the person's grant
            # is not at fault — nothing is marked.
            raise JefactureRenewalRefused(
                f"{e} — the access could not be renewed; if it persists, tell the "
                "instance operator (application coordinates).") from e
        else:
            if grant.refresh_token != refresh_token:
                credentials_store.set_credential(
                    ligne[0], ligne[1], CONNECTOR, grant.refresh_token,
                    set_by=row.get("set_by"), meta=row.get("meta") or {},
                    account=ligne[2], conn=conn)
            _garder(ligne, grant)
            refus = None
    if refus is None:
        connector_health.record_health(CONNECTOR, ligne, True, None)
        return grant.access_token
    # A refusal is checked against the row before it marks anything: a secret that
    # changed meanwhile is a reconnection, not a dead grant.
    if not reprise and _lire(ligne)["secret"] != refresh_token:
        return _renouveler(ligne, refresh_token, reprise=True)
    message = ("jefacture.com no longer accepts this sign-in (expired or revoked): "
               f"{_RECONNECTER}.")
    connector_health.mark_rejected(ligne[0], ligne[1], CONNECTOR, ligne[2], message)
    raise JefactureReauthRequired(message) from refus


def _ligne_de(sub: str):
    from .. import access  # lazy: avoids any import cycle at boot

    rc = access.resolve_credential(CONNECTOR, want="byo", sub=sub)
    return (rc.entity_type, rc.entity_id, rc.account or ""), rc.key


def access_token_for(sub: str) -> str:
    """A valid access token for the person, renewed if it expires within a minute.
    Raises a `McpError` (not connected), `RuntimeError` (core missing, application not
    configured), `JefactureRenewalRefused` (application refused) or
    `JefactureReauthRequired` (dead authorization: the row is marked, the card says
    "reconnect")."""
    ligne, refresh_token = _ligne_de(sub)
    return _en_cache(ligne, refresh_token) or _renouveler(ligne, refresh_token)


def oublier_jeton(sub: str) -> None:
    """Drops the person's cached access token after the API refused it (401): the next
    call renews instead of replaying it until its announced expiry."""
    ligne, refresh_token = _ligne_de(sub)
    with _VERROU:
        _JETONS.pop(_cle(ligne, refresh_token), None)


# --- what the card displays --------------------------------------------------

def _row(org_id: int, sub: str) -> Optional[dict]:
    entity_type, entity_id = _scope(org_id, sub)
    return credentials_store.get_credential_with_meta(entity_type, entity_id, CONNECTOR)


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
        return "jefacture.com app to be configured by the operator"
    etat = _link_state(sub)
    if not etat.linked:
        return "Sign in with jefacture.com"
    if etat.health_ko:
        return "Sign-in expired — reconnect"
    return None


status_hints.register(CONNECTOR, _etape_manquante)
