"""Pipecorn tool — waterfall contact enrichment (mobile phone, work email), asynchronous,
and the account's balance.

Wraps `oto.tools.pipecorn.PipecornClient`. The key comes from the cascade (own key >
platform grant) via `access.resolve_credential("pipecorn")`.

ONE tool, `pipecorn` (LECTURE): a lookup changes nothing at Pipecorn, it spends
credits — like any enrichment read here. No `pipecorn_act` (`tests/test_convention_act.py`).

**Asynchronous.** `op="enrich"` SUBMITS the lookup (Pipecorn queues it and answers at
once with an `enrichment_id`); `op="result"` reads it back (free), `pending` until
Pipecorn's own cascade of providers is done. `waterfall_phone` polls it like its other
asynchronous steps (`waterfall/telephone.py`).

**A third party's personal email is refused**, on every key, before anything is resolved
or sent (`pipecorn_personal_email_refused`): the client refuses it too.

**Metering** (facts, no price), UNCONDITIONAL — own key or shared key: the consumer
filters on `key_mode`. Pipecorn bills per result FOUND, in its credits, when the lookup
finishes: 3 a work email, 30 a phone — a phone only when a MOBILE is found
(`mobile` or `fixed_or_mobile`); a miss costs nothing. Nothing in the answer says what
it cost: the client reads it from what was found (`found_kinds`, `credits_spent`,
bounded by what was asked — `find`, echoed by the submission and passed back to the
result).
- the submission traces `quantity=0` (nothing is spent yet), and on the shared key
  `units=1` (one lookup against the grant's quota);
- a read still `pending`, or `failed`, traces `quantity=0` (and `units=0` on the shared
  key);
- a FINISHED read traces `quantity` = 1 when something asked was found, else 0 (never
  absent: the aggregated view reads an absence as 1), `provider_credits` = the
  Pipecorn credits spent, and the three `found_work_emails` / `found_personal_emails`
  (always 0) / `found_phones` together (the billing lens reads them all or nothing).
  Every finished read of the same lookup traces the same figures: the consumer counts
  it once by its `enrichment_id` (the read's argument, the lens's `job_id`,
  `db.usage.BILLABLE_JOB_ARGS`).

**Units budget** (`access/credits.py`): the submission is in the perimeter, as the
connectors library's `pipecorn.submit_enrichment`, PRICED PER KIND FOUND — the units
`work_emails` and `phones`, each under its own scale line, like FullEnrich. On a
re-billed key (the platform's, or the primary tenant's) its worst case, one find per
kind asked, is reserved before the lookup is sent; the read that finds it FINISHED
settles each kind at what was found (`credits_releve.regler_releve`), a `failed` one
releases it, and one never read is settled at the amount reserved when its hold expires
(`credits_echeances`). On the customer's own key nothing is reserved: Pipecorn bills
that key.

**The shared key** serves a PHONE lookup only (the first step of `waterfall_phone`):
an email and the balance ask for the customer's own key, refused BEFORE anything is
spent. A result is read with the key that submitted it.

Client calls are written out (`client.submit_enrichment(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, session_org
from ..access import credits as budget
from ..access import credits_releve
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import LECTURE

_NAME = "pipecorn"

#: What a lookup may ask for, and what the shared key serves of it.
FINDS = ("phone", "email")
PLATFORM_FINDS = frozenset({"phone"})

#: The kinds a lookup asks, as the units budget's units (FullEnrich's names): what is
#: priced per lookup FOUND, kind by kind.
_NATURES = {"email": "work_emails", "phone": "phones"}

#: Why a lookup ended `failed`: credits ran out while it ran, nothing was found.
_FAILED = {
    "insufficient_credits": "the Pipecorn balance ran out while it ran",
    "workspace_limit_exceeded": "the key's weekly credit limit was reached while it ran",
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refus(code: str, message: str, **data) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=f"Refusal `{code}`: {message}",
                              data={"code": code, "retryable": False, **data}))


def _finds(find) -> list[str]:
    return list(dict.fromkeys(find or ["phone"]))


def _pire_par_nature(args: dict) -> dict:
    """The worst case of a submission, per kind asked: one find of each."""
    from oto.connectors.cost import CostError
    finds = _finds(args.get("find"))
    if (args.get("op") or "enrich") != "enrich" or any(f not in _NATURES for f in finds):
        raise CostError(f"pipecorn: no submission, or a kind without a unit in {finds!r}")
    return {_NATURES[f]: 1 for f in finds}


def _releve(enrichment_id: str) -> tuple[str, dict]:
    """The collection of a submitted lookup, as `cost.collection_of` names it."""
    return "pipecorn.get_enrichment", {"enrichment_id": enrichment_id}


def _code(e) -> str:
    return e.body.get("code", "") if isinstance(e.body, dict) else ""


def _error(e) -> str:
    if isinstance(e.body, dict):
        return str(e.body.get("error") or e.body)
    return str(e.body or "")


def _upstream_message(e, is_platform: bool = False) -> str:
    """Turn a Pipecorn refusal into an actionable message, by status and `code`."""
    status = e.status_code
    if status == 402 or _code(e) == "insufficient_credits":
        if is_platform:
            return ("Pipecorn: the shared key's credit balance is empty (402) — nothing "
                    "was spent. Tell the instance's administrator; do not retry.")
        return ("Pipecorn: not enough credits left on your account for this lookup (402) "
                "— nothing was spent. Top up in Pipecorn, then retry.")
    if status == 403 and _code(e) == "workspace_limit_exceeded":
        return ("Pipecorn: this key's weekly credit limit for its workspace is reached "
                "(403) — a workspace admin raises it in Pipecorn, or wait for the week "
                "to reset.")
    if status == 401:
        return ("Pipecorn rejected the API key (401): copy it again from Settings → API "
                "keys; a rotated key stops working at once.")
    if status == 403:
        return f"Pipecorn refused this call (403): {_error(e)}"
    if status == 404:
        return ("Pipecorn has no such lookup for this key (404): check the "
                "`enrichment_id` — a lookup is read with the key that submitted it.")
    if status in (400, 422):
        return f"Pipecorn rejected the request ({status}): {_error(e)}"
    if status == 429:
        return "Pipecorn: too many requests (429) — retry in a few seconds."
    if status in (500, 502, 503, 504):
        return (f"Pipecorn failed or timed out (HTTP {status}). A read or the balance "
                "can be retried; a submitted lookup may have been accepted: do not submit "
                "it again blindly.")
    return f"Pipecorn refused the request (HTTP {status}): {_error(e)}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test the connection" probe: the balance (`GET /credits`), free — it also says
    whether enrichment credits are left. An unreadable answer or balance is an outage,
    never a refused key nor a healthy account."""
    from oto.tools.common.credentials import MissingCredential
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.pipecorn import PipecornClient, PipecornUnreadable

    try:
        balance = PipecornClient(api_key=fields.get("key")).probe()
    except MissingCredential as e:
        raise connector_verify.NonAutorise(str(e))
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Pipecorn HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Pipecorn HTTP {e.status_code}: {e.body}")
    except PipecornUnreadable as e:
        raise RuntimeError(f"Pipecorn: unreadable answer to the balance read — {e}")
    left = connector_verify.nombre_lu(
        ((balance if isinstance(balance, dict) else {}).get("credits") or {}).get("enrichment"))
    if left is None:
        raise RuntimeError("Pipecorn answered without a readable enrichment balance: "
                           f"{str(balance)[:200]}")
    if left <= 0:
        raise connector_verify.QuotaEpuise("Pipecorn: no enrichment credits left.")


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.pipecorn import (PipecornClient, PipecornUnreadable, credits_spent,
                                    found_kinds)

    connector_verify.register(_NAME, _verify, couvre=connector_verify.AUTH_QUOTA)
    budget.declarer(_NAME, _NAME, "submit_enrichment", unites=_pire_par_nature)

    def _client(op: str, finds: list[str]):
        """`(client, rc)`. Only a submission spends: it resolves with the usage checks
        (and, on a re-billed key, reserves its worst case); a read, the balance and the
        account resolve without them. On the platform key, a submission of anything but
        a phone, and the balance, are refused BEFORE a credit is spent, naming what the
        shared key serves."""
        rc = access.resolve_credential(_NAME, check_usage=(op == "enrich"))
        refused = (op == "enrich" and not set(finds) <= PLATFORM_FINDS) or \
            op in ("credits", "account")
        if rc.is_platform and refused:
            from ..auth.hooks import current_user_sub_from_token
            from .. import links
            sub = current_user_sub_from_token()
            what = "the balance" if op != "enrich" else \
                ", ".join(f for f in finds if f not in PLATFORM_FINDS)
            raise _bad(
                f"pipecorn op={op!r} ({what}) needs your own Pipecorn key: the shared key "
                "here covers phone lookups only. Set your own key"
                f"{links.ou_poser_la_cle(sub, org=access.current_org(sub), connecteur=_NAME)}.")
        return PipecornClient(api_key=rc.key), rc

    def _run(fn, is_platform: bool = False):
        try:
            return fn()
        except ValueError as e:
            # The client's own checks, before anything is sent: nothing consumed.
            budget.rien_envoye()
            raise _bad(str(e))
        except PipecornUnreadable as e:
            raise _refus("pipecorn_unreadable_answer",
                         f"Pipecorn answered something that is not JSON — nothing could "
                         f"be read from it ({e}). Retry later; a submitted lookup may "
                         "have been accepted: read it back rather than submit it again.")
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e, is_platform))

    @mcp.tool(name="pipecorn", annotations=LECTURE)
    def pipecorn(
        op: Literal["enrich", "result", "credits", "account"] = "enrich",
        find: Optional[list[Literal["phone", "email", "personal_email"]]] = None,
        linkedin_url: Optional[str] = None,
        firstname: Optional[str] = None,
        lastname: Optional[str] = None,
        domain: Optional[str] = None,
        company_name: Optional[str] = None,
        phone_type: Optional[Literal["all", "mobile"]] = None,
        phone_country_codes: Optional[list[str]] = None,
        enrichment_id: Optional[str] = None,
    ) -> dict:
        """Pipecorn — find a person's mobile phone or work email (asynchronous: submit,
        then read the result); the account's balance.

        Pipecorn queries many data providers one after the other and stops at the first
        that answers. You only pay for what is found, in Pipecorn credits: 3 a work
        email, 30 a phone — and a phone only when a MOBILE is found. A miss costs
        nothing. A third party's personal email is never looked up: `personal_email`
        is refused, on any key.

        op="enrich" (default) — SUBMIT one person; answers at once with
          `enrichment_id`. `find`: `phone`, `email` or both (default `["phone"]`).
          - `phone` needs `linkedin_url` (a profile URL).
          - `email` takes `linkedin_url`, or `firstname` + `lastname` + `domain` or
            `company_name`.
          - `phone_type="mobile"` keeps looking until a provider has a mobile and
            drops landlines; `phone_country_codes=["FR"]` keeps phones to countries.
        op="result" — read it back: `enrichment_id`, and the same `find` as the
          submission. `done: false` while Pipecorn is still looking — read again in a
          few seconds (usually under a minute; reads are free). Done: `email`,
          `email_status` (deliverable or catch-all), `phone` (numbers, as Pipecorn
          writes them — with or without `+`), `phone_details` [{number, type}], and
          `pipecorn_credits`, what the lookup cost. A miss is empty fields, not an
          error.
        op="credits" — the balance: `credits.enrichment` is what lookups spend.
        op="account" — the plan and the email behind the key.

        For a person's phone across several providers, prefer `waterfall_phone`: it
        submits and reads this for you.

        Examples —
          pipecorn(find=["phone"], linkedin_url="https://www.linkedin.com/in/jane-doe/",
                   phone_type="mobile")
          pipecorn(op="result", enrichment_id="…", find=["phone"])
        """
        given = {"find": find, "linkedin_url": linkedin_url, "firstname": firstname,
                 "lastname": lastname, "domain": domain, "company_name": company_name,
                 "phone_type": phone_type, "phone_country_codes": phone_country_codes,
                 "enrichment_id": enrichment_id}
        takes = {"enrich": set(given) - {"enrichment_id"},
                 "result": {"enrichment_id", "find"}}.get(op, set())
        stray = sorted(k for k, v in given.items() if v is not None and k not in takes)
        if stray:
            raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                       "Nothing was done.")
        finds = _finds(find)
        if "personal_email" in finds:
            raise _refus("pipecorn_personal_email_refused",
                         "a third party's personal email is never looked up, on any "
                         "key. Ask for `email` (work email) or `phone`. Nothing was done.")
        client, rc = _client(op, finds)

        if op == "credits":
            return _run(lambda: client.get_credits())
        if op == "account":
            return _run(lambda: client.get_account_info())

        if op == "enrich":
            budget.envoi()
            submitted = _run(lambda: client.submit_enrichment(
                finds, linkedin_url=linkedin_url, firstname=firstname,
                lastname=lastname, domain=domain, company_name=company_name,
                phone_type=phone_type, phone_country_codes=phone_country_codes),
                rc.is_platform)
            budget.reponses([submitted])
            # Nothing is spent until the lookup finishes: a TRACED zero, not an absence.
            if rc.is_platform:
                session_org.note_call_trace(quantity=0, units=1)
            else:
                session_org.note_call_trace(quantity=0)
            ident = submitted.get("enrichment_id") if isinstance(submitted, dict) else None
            if not ident:
                raise _bad("Pipecorn accepted the lookup but returned no enrichment_id: "
                           f"nothing can be read back ({str(submitted)[:200]}).")
            return {"enrichment_id": ident, "status": "pending", "find": finds,
                    "next_step": (f"Call pipecorn(op='result', enrichment_id='{ident}', "
                                  f"find={finds}) in a few seconds; reads are free.")}

        if not enrichment_id:
            raise _bad("op='result' needs the `enrichment_id` the submission returned.")
        answer = _run(lambda: client.get_enrichment(enrichment_id), rc.is_platform)
        status = answer.get("status") if isinstance(answer, dict) else None
        refacture = budget.rang_refacture(rc.mode, rc.entity_id)
        if status != "finished":
            if rc.is_platform:
                session_org.note_call_trace(quantity=0, units=0)
            else:
                session_org.note_call_trace(quantity=0)
        if status == "failed":
            if refacture:
                # No result will come: what the submission reserved is released.
                credits_releve.liberer_releve(*_releve(enrichment_id))
            code = answer.get("code") or ""
            why = _FAILED.get(code, f"Pipecorn stopped it ({code or 'no reason given'})")
            raise _bad(f"Pipecorn: lookup {enrichment_id} failed — {why}; nothing was "
                       "found or billed. Top up (or have the limit raised), then submit "
                       "it again.")
        if status != "finished":
            return {"done": False, "enrichment_id": enrichment_id,
                    "status": status or "pending",
                    "next_step": "Still looking: read it again in a few seconds."}
        found = found_kinds(answer, finds)
        spent = credits_spent(answer, finds)
        trace = {"quantity": 1 if any(found.values()) else 0, "provider_credits": spent,
                 "found_work_emails": int(found.get("email", False)),
                 "found_personal_emails": 0,
                 "found_phones": int(found.get("phone", False))}
        if rc.is_platform:
            trace["units"] = 0
        session_org.note_call_trace(**trace)
        if refacture:
            # The hold of the submission settles kind by kind at what was FOUND.
            credits_releve.regler_releve(
                *_releve(enrichment_id),
                {_NATURES[k]: int(hit) for k, hit in found.items()})
        # A third party's personal email never leaves here, even one Pipecorn returns
        # unasked (`pipecorn_personal_email_refused`).
        answer = {k: v for k, v in answer.items() if not k.startswith("personal_email")}
        return {**answer, "done": True, "pipecorn_credits": spent}
