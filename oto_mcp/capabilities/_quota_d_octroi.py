"""A platform key billed per use is granted WITH a daily quota — never unlimited.

A connector that declares `grant_quota_unit` (`providers/_model.py`) refuses a grant of
its platform key without a positive `daily_quota`: absent, the grant falls back on
`default_quota`, and 0 means UNLIMITED on our account. The rule lives in
`credentials_store.exiger_quota_d_octroi` (played by `platform_grant` on every path);
this module plays it BEFORE a console gesture writes anything — an option mark set and
then a grant refused would leave half a gesture behind — and turns it into the named
refusal `grant_quota_required`, the same on every surface.
"""
from __future__ import annotations

from typing import Optional

from .. import credentials_store, providers
from ._types import AuthzDenied, DeclaredError

CODE = "grant_quota_required"


#: What a gesture that carries NO quota adds to the refusal: where the quota is passed.
#: French, like the sentence it follows (`credentials_store.exiger_quota_d_octroi`).
SANS_QUOTA = (" Ce geste ne porte pas de quota : accorde la clé par `oto_admin_key_grant` "
              "(`platform.org.grant_key`, `platform.key.grant`, `platform.group.grant_key`) "
              "avec `daily_quota`.")


def exiger(provider: str, daily_quota: Optional[int], suite: str = "") -> None:
    """Refuses (400 `grant_quota_required`) a grant of `provider`'s platform key that the
    connector does not allow without a positive quota; does nothing otherwise. `suite`:
    what the gesture adds to the sentence (`SANS_QUOTA` for one that carries none)."""
    try:
        credentials_store.exiger_quota_d_octroi(provider, daily_quota)
    except credentials_store.QuotaDOctroiRequis as e:
        # The code written out, not `CODE`: the declared-refusal guard reads the raise
        # literally (`tests/_refus_atteignables.py`); a name there reads as computed.
        raise AuthzDenied(400, "grant_quota_required", f"{e}{suite}") from e


def _unites() -> str:
    """`elevenlabs` (ElevenLabs credits), … — derived from the registry, never copied."""
    return ", ".join(f"`{c.name}` ({c.grant_quota_unit})"
                     for c in sorted(providers.REGISTRY.values(), key=lambda c: c.name)
                     if c.grant_quota_unit)


#: The sentence the grant tools serve: which connectors require the quota, in which unit.
AIDE = (f"`daily_quota` is REQUIRED (> 0) for {_unites()}, counted in that unit — absent "
        "or 0 (unlimited) is refused with `grant_quota_required`.")


def declaration() -> DeclaredError:
    """The published declaration of the refusal, for every capability that raises it."""
    return DeclaredError(400, CODE, f"a platform-key grant without the quota its connector "
                                    f"requires: {AIDE}")
