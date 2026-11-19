"""The coordinates of an OAuth application that belongs to the INSTANCE — one reader.

Microsoft, jefacture, instagram_meta, hubspot, slack, linkedin_ads and meta_ads sign their
consent flows with an application the instance operator registered once at the provider.
Its coordinates come in two kinds, stored in two places:

- **public settings** (`client_id`, `app_id`, `config_id`…): PLATFORM scope of
  `connector_settings` — they travel in every authorization URL anyway;
- **secrets** (`client_secret`, `app_secret`, `user_client_secret`, `signing_secret`…):
  the vault, encrypted by the master key, PLATFORM scope under `app:instance`
  (`credentials_store.set_app_secret`) — the publisher app's storage, never served by
  the cascade (`tests/test_editor_app.py`).

The split is decided by the NAME of the key (`connector_settings.a_la_forme_d_un_secret`,
a closed list of patterns), the same predicate that makes `connector_settings` refuse a
secret and that routes the admin console's `op=set` to the vault. A secret is read from
the vault ONLY: a stray plain row of the same key in `connector_settings` is never read
(no fallback) — a missing secret is reported missing, by the connector's own refusal.

COLD reads, off the tool-call path: start of a flow, return of a consent, a token renewal,
a signed event — the frequencies the modules already documented for their
`connector_settings` reads.
"""
from __future__ import annotations

from typing import Sequence

from .. import credentials_store
from ..db import connector_settings as store


def _partager(connector: str, cles: Sequence[str]) -> tuple[list[str], list[str]]:
    secretes = [k for k in cles if store.a_la_forme_d_un_secret(connector, k)]
    return [k for k in cles if k not in secretes], secretes


def _publics(connector: str, cles: Sequence[str]) -> dict:
    if not cles:
        return {}
    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == connector
            and r["key"] in cles}


def reglages(connector: str, cles: Sequence[str]) -> dict:
    """`{key: value}` of the keys `cles` that are set (empty values left out): public
    ones from `connector_settings`, secret ones decrypted from the vault. A vault row that
    does not decrypt RAISES — an unreadable secret is not an absent one."""
    publics, secretes = _partager(connector, cles)
    out = {k: v for k, v in _publics(connector, publics).items() if v}
    if secretes:
        out.update({k: v for k, v in
                    credentials_store.get_app_secrets(connector, secretes).items() if v})
    return out


def manquantes(connector: str, cles: Sequence[str]) -> list[str]:
    """Those of `cles` that are not set, in their order — presence only, nothing is
    decrypted (what a card asks before offering "Connect")."""
    publics, secretes = _partager(connector, cles)
    poses = {k for k, v in _publics(connector, publics).items() if v}
    if secretes:
        # The PLATFORM-scope inventory the cascade already reads once per call (memoized):
        # a card's presence costs no query of its own.
        poses |= {i["account"] for i in
                  credentials_store.list_all_platform_instances().get(connector, [])
                  if i["label"] == credentials_store.INSTANCE_APP_LABEL}
    return [k for k in cles if k not in poses]
