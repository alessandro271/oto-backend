"""Per-org FAIRNESS on a shared platform key (F-051).

A platform key is ONE account at its provider, shared by every org it serves, with ONE
rate limit there. Nothing bounded how fast a single org could draw on it: the daily
quota (`quotas.quota_for`) is per person and per day, often lifted
(`platform_unmetered`), and says nothing about a burst. One org looping on a shared
key could exhaust the provider's rate limit — or its credit — for every other org.

This module caps the calls ONE org makes on ONE shared key per fixed window, counted
in the database (`db/platform_key_rate.py`) because the server runs as several
processes that share the database and nothing else.

- **What is counted**: one per platform-key resolution of a tool execution
  (`check_usage=True`), not `units` — the limit protects the provider's request
  rate; volume is the quota's job. Debited at RESOLUTION, like `tenant_budget`
  (a call that then fails at the provider still counts; there is no single
  post-call seam to count on success).
- **Who is counted**: the org whose keys the call consumes
  (`heritage.org_des_cles`), every member together; a call without an org counts
  for the person (`user:<sub>`). The anonymous endpoint counts for the project's org.
- **Rollout**: `OTO_PLATFORM_KEY_ORG_RATE` = `enforce` | `report` (DEFAULT: logs
  each window an org crosses the limit, refuses nothing) | `off` (no counting at
  all). An unknown value reads `report`.
- **Limits**: `OTO_PLATFORM_KEY_ORG_RATE_LIMIT` calls (default 120) per
  `OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S` seconds (default 60);
  `OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES` = `serper=600,aiark=30` per key carrier,
  `0` = no limit for that connector.

The counter failing never refuses a call: it is logged, and the call goes through.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from mcp.types import ErrorData, INVALID_PARAMS

from .. import providers
from ..db import platform_key_rate as db_rate
from ..mcp_errors import McpError

logger = logging.getLogger(__name__)

#: The named code of the refusal — tested by callers, never the sentence.
CODE = "platform_key_org_rate_limited"

_ENV_MODE = "OTO_PLATFORM_KEY_ORG_RATE"
_ENV_LIMIT = "OTO_PLATFORM_KEY_ORG_RATE_LIMIT"
_ENV_WINDOW = "OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S"
_ENV_OVERRIDES = "OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES"
_MODES = ("enforce", "report", "off")
DEFAULT_LIMIT = 120
DEFAULT_WINDOW_S = 60


def mode() -> str:
    """Read on each call (a flip bites at the next restart, without code). Unknown
    value = `report`, the default: measure before biting."""
    m = (os.environ.get(_ENV_MODE) or "report").strip().lower()
    return m if m in _MODES else "report"


def _entier(raw: Optional[str], defaut: int) -> int:
    try:
        return max(0, int((raw or "").strip()))
    except ValueError:
        return defaut


def window_s() -> int:
    return max(1, _entier(os.environ.get(_ENV_WINDOW), DEFAULT_WINDOW_S) or DEFAULT_WINDOW_S)


def limit_for(carrier: str) -> int:
    """Calls per window for one org on `carrier`'s shared key; `0` = no limit."""
    for paire in (os.environ.get(_ENV_OVERRIDES) or "").split(","):
        nom, egal, valeur = paire.partition("=")
        if egal and nom.strip().lower() == carrier:
            return _entier(valeur, DEFAULT_LIMIT)
    raw = os.environ.get(_ENV_LIMIT)
    return DEFAULT_LIMIT if raw is None else _entier(raw, DEFAULT_LIMIT)


def _porteur(sub: Optional[str], org: Optional[int]) -> Optional[str]:
    if org is not None:
        return f"org:{org}"
    return f"user:{sub}" if sub else None


def refusal(carrier: str, used: int, limit: int, window: int, hint: str = "") -> McpError:
    return McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(f"Too many calls on oto's shared `{carrier}` key from this org: {used} "
                 f"in the current {window}-second window, limit {limit} per org. The key "
                 f"is shared by every org; retry in a minute, or set your own "
                 f"`{carrier}` key{hint} to lift the limit."),
        data={"code": CODE, "connector": carrier, "used": used, "limit": limit,
              "window_s": window}))


def enforce(provider: str, sub: Optional[str], org: Optional[int], *,
            hint: Callable[[], str] = lambda: "") -> None:
    """Count this platform-key call for its org, and refuse it (in `enforce`) past the
    org's limit for the window. `hint` = where to set one's own key, built only when
    refusing."""
    m = mode()
    if m == "off":
        return
    carrier = providers.credential_provider(provider)
    limit = limit_for(carrier)
    qui = _porteur(sub, org)
    if not limit or qui is None:
        return
    window = window_s()
    try:
        used = db_rate.compter(qui, carrier, window)
    except Exception:
        # Fail-open, loudly: a broken counter must not take every platform key down.
        logger.exception("platform-key rate: counter failed for %s on %s — call served",
                         qui, carrier)
        return
    if used <= limit:
        return
    if used == limit + 1:   # once per window and per org, not once per call
        logger.warning("platform-key rate (%s): %s crossed %d calls/%ds on `%s`",
                       m, qui, limit, window, carrier)
    if m == "enforce":
        raise refusal(carrier, used, limit, window, hint())
