"""Which account each connector of a cascade runs on, when the caller holds several.

A cascade calls several connectors, each under ITS key: with two keys of one connector
(two lemlist workspaces), the caller names which with `_accounts={"lemlist": "<account>"}`
— one account per CONNECTOR, never per step: `waterfall_person` has two lemlist steps
(`lemlist_db`, `lemlist`), and both run on the account named for lemlist. A plain
`_account` is taken only when the steps that run sit on ONE connector that takes an
account (`providers=["lemlist_db", "lemlist"]`); across several it would name the same
account for every connector at once, so it is refused, naming `_accounts`.

Judged after the plan and BEFORE any step is called, `dry_run` included: a name that is
not a connector of these steps, or an account that connector does not have, is refused
by name — never a step skipped and the next provider paid instead. Then every call of a
step on that connector (submit, pages, reveal, collect) carries `_account`, read by
`meta.executer_cible` like any other call's (`call_axes.ACCOUNT`). Nothing named = the
calls are made exactly as before.

The two axes are posed by the call (`call_axes.STEP_ACCOUNTS`, `CASCADE_ACCOUNT`) in
ContextVars only this module reads — never in `_CALL_ACCOUNT`, which every step's key
resolution would read."""
from __future__ import annotations

import contextvars
import re
from typing import Iterable, Optional

from .. import session_org
from ..mcp_errors import McpError
from . import ordre
from .etape import Step
from .valeurs import _bad, _refused

#: `{vault carrier: account}` of the cascade running in this task, read by `appel`.
_CHOIX: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "waterfall_step_accounts", default=None)


def _toutes(steps: Iterable[Step]):
    for s in steps:
        yield s
        yield from _toutes(s.variants)


def _connecteurs(steps: Iterable[Step]) -> dict[str, tuple[str, bool]]:
    """`{name a caller may write: (vault carrier, identities?)}` for the steps whose call
    takes an account: a keyed connector that reads `_account` — not open data (`fr_get`
    resolves no key, `Step.keyless`). The connector's name and its carrier are both
    accepted (`linkedin_unipile` and `unipile` name the same accounts). `identities`: the
    carrier's accounts are operated identities (`Connector.personal_cross_org`, unipile),
    resolved by the step itself, not by the key cascade."""
    from .. import call_axes, providers
    from ..tool_visibility import namespace_of
    out: dict[str, tuple[str, bool]] = {}
    for s in _toutes(steps):
        porteur = ordre._carrier(s.tool)
        if (s.keyless or porteur is None or porteur not in providers.CREDENTIAL_PROVIDERS
                or not call_axes.accepts_account_axis(s.tool)):
            continue
        con = providers.connector_for_namespace(namespace_of(s.tool))
        identites = bool(con is not None and con.personal_cross_org)
        out[porteur] = (porteur, identites)
        if con is not None:
            out.setdefault(con.name, (porteur, identites))
    return out


def _verifier(sub: Optional[str], porteur: str, compte: str) -> None:
    """The account exists for this caller at a tier they reach — else refused, by name.
    Under a throwaway call trace: a resolution notes the key it found (`key_mode`, the
    vault row) on the trace, which bills and names the payer of the CALL — this one
    is a check, not a call. Only the account's absence refuses: a connector with no
    usable key at all is the step's to say (`no_key`, on the receipt), as before."""
    from .. import access
    jeton = session_org.set_call_trace({})
    try:
        access.resolve_credential(porteur, sub=sub, account=compte,
                                  emit_on_failure=False, check_usage=False)
    except access.CompteIntrouvable:
        raise _refused("account_not_found", (
            f"`_accounts`: no {access.account_noun(porteur)} `{compte}` for `{porteur}` "
            "here — list yours with oto_identity(op='list'). Nothing was called."))
    except McpError:
        return
    finally:
        session_org.reset_call_trace(jeton)


def choisir(sub: Optional[str], steps: Iterable[Step], order: list[Step]) -> dict[str, str]:
    """SYNC (the vault): the call's `{carrier: account}`, judged — `{}` when it names
    none. `steps` = every step of the cascade (what `_accounts` may name), `order` = the
    ones that will run (what a plain `_account` must be unambiguous over)."""
    par_connecteur = session_org.current_call_step_accounts()
    seul = session_org.current_call_cascade_account()
    if not par_connecteur and not seul:
        return {}
    if par_connecteur and seul:
        raise _bad("Pass `_accounts` (one account per connector) or `_account`, not both.")
    valides = _connecteurs(steps)
    if par_connecteur:
        inconnus = sorted(k for k in par_connecteur if k not in valides)
        if inconnus:
            raise _bad(
                f"`_accounts`: {', '.join(inconnus)} — not a connector of this "
                "waterfall's steps that takes an account; one of "
                f"{', '.join(sorted({p for p, _ in valides.values()}))}. Nothing was "
                "called.")
        choix: dict[str, str] = {}
        for nom, compte in par_connecteur.items():
            porteur = valides[nom][0]
            if choix.get(porteur, compte) != compte:
                raise _bad(f"`_accounts` names two accounts for `{porteur}`: one only.")
            choix[porteur] = compte
    else:
        engages = sorted({p for p, _ in _connecteurs(order).values()})
        if len(engages) != 1:
            raise _bad(
                "`_account` is ambiguous on a waterfall: "
                + (f"its steps run on {len(engages)} connectors ({', '.join(engages)})"
                   if engages else "none of its steps takes an account")
                + ". Name one account per connector with `_accounts`, e.g. "
                "`_accounts={\"lemlist\": \"<account>\"}`, or narrow `providers` to one "
                "connector's steps. Nothing was called.")
        choix = {engages[0]: str(seul)}
    for porteur, compte in choix.items():
        if not valides.get(porteur, (porteur, False))[1]:
            _verifier(sub, porteur, compte)
    return choix


def poser(choix: dict[str, str]) -> contextvars.Token:
    return _CHOIX.set(dict(choix) or None)


def lever(jeton: contextvars.Token) -> None:
    _CHOIX.reset(jeton)


def compte_de(tool: str) -> Optional[str]:
    """The account named for the connector of `tool` in the running cascade, or None."""
    choix = _CHOIX.get()
    if not choix:
        return None
    return choix.get(ordre._carrier(tool) or "")


def avec_compte(tool: str, args: dict) -> dict:
    """A copy of a step call's arguments, with `_account` when one is named for its
    connector — the token `meta.executer_cible` reads, as on any call."""
    compte = compte_de(tool)
    return {**args, "_account": compte} if compte else dict(args)


#: What a refusal "name the account" says to do on a direct call (`access.cascade`,
#: `tools/meta._compte_a_nommer`) — on a waterfall step, the waterfall's token does it.
_PASSE_ACCOUNT = re.compile(r"[Pp]ass `_account=\"<name>\"` on this call")


def conseil_cascade(tool: str, message: Optional[str]) -> str:
    """A step's "name the account" refusal, worded for the waterfall that called it."""
    porteur = ordre._carrier(tool) or "<connector>"
    jeton = f"pass `_accounts={{\"{porteur}\": \"<name>\"}}` on the waterfall call"
    texte = message or ""
    if _PASSE_ACCOUNT.search(texte):
        return _PASSE_ACCOUNT.sub(jeton, texte)
    return f"{texte} To choose it, {jeton}.".strip()
