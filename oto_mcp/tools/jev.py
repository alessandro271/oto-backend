"""Jev tools: typed answers instead of a model turn (TypeSafe, via OpenRouter).

Wraps `oto.tools.jev.client.JevClient`. `jev_ask` = one state, whole rubric in one call;
`jev_items` = many states, same rubric, nothing written.

⚠️ TENANT key only (see `providers/jev.py`); a closer key is refused, naming who removes it.
Billing: `quantity` = the real upstream cost in micro-dollars (like `serper`), not a call count.
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Optional

import requests
from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, credentials_store, session_org
from ..access.resolve import CredentialUnavailable
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError

#: The only cascade rung this tool serves. A closer rung wins the cascade first and is refused.
RANGS_SERVIS = ("tenant",)

#: Who can remove a key that shadows the tenant's, per rung.
QUI_RETIRE = {"org": "an org admin",
              "group": "a team admin",
              "user": "its owner, on their account page"}

#: Max states per `jev_items` (measured 28/09/2026: 50 in flight, no 429).
MAX_ITEMS = 200
#: Default in-flight calls, below the measured rate: these are server pool threads.
PARALLELE_DEFAUT = 10
PARALLELE_MAX = 50

#: Max size of ONE state (UTF-8 JSON bytes), ~4k tokens; caps a call's spend (input is billed).
MAX_ETAT_OCTETS = 16_000

#: Read timeout per answer (connect keeps the client's 10 s); answers take ~0.5 s.
LECTURE_S = 15
#: Hard cap on the REST path (`capabilities/tools_me.py`), same as `clay`.
REST_CALL_LIMIT_S = 45.0
#: Batch send window: REST cap − worst in-flight call (10 + 15 s) − margin. Unsent states go to `retry`.
LOT_FENETRE_S = REST_CALL_LIMIT_S - (10 + LECTURE_S) - 5.0


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status in (401, 403):
        return (f"OpenRouter rejected the key (HTTP {status}): the tenant's Jev key is "
                "invalid or revoked. A tenant admin must set it again.")
    if status == 402:
        return ("OpenRouter credits exhausted (402): a tenant admin must top up the "
                "tenant's key.")
    if status == 429:
        return "Jev: too many requests (429). Retry shortly."
    if status == 400:
        # Upstream body verbatim: it names the faulty question or `max_tokens_exceeded`.
        return f"Jev rejected the request (400): {e.body}"
    if status in (500, 502, 503, 504):
        return f"Jev is temporarily unavailable (HTTP {status}). Retry later."
    return f"Jev rejected the request (HTTP {status}): {e.body}"


def _microdollars(cout: Optional[float]) -> int:
    """Call cost in micro-dollars, rounded UP; no declared cost bills 0.

    ⚠️ The `round` before `ceil` matters: a batch cost is a float sum, and
    `3 × 1e-05` = 3.0000000000000004e-05, which bare `ceil` would bill as 31 µ$."""
    return math.ceil(round(float(cout) * 1e6, 6)) if cout else 0


def _verify(fields: dict, config: dict | None = None,  # noqa: ARG001
            instance: tuple | None = None) -> None:
    """"Test connection" probe: the smallest possible call (one word, one yes/no).

    ⚠️ A key on any rung but TENANT fails here without a call: it would be refused at use.
    Without `instance` (check before saving), only the key is tested."""
    if instance is not None and instance[0] != credentials_store.TENANT:
        raise ValueError(
            f"a `jev` key set at the '{instance[0]}' level is not used: Jev only runs "
            "on the TENANT key, which this one would shadow.")
    from oto.tools.jev.client import JevClient
    JevClient(api_key=fields["key"]).decide(
        {"word": "test"},
        {"ok": {"type": "noul", "instructions": "Is this word 'test'?",
                "criteria": {"true": "the word is test", "false": "another word"}}},
        timeout=20)


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.jev.client import JevClient

    connector_verify.register("jev", _verify)

    def _client(units: int = 1) -> JevClient:
        """Client on the tenant key; `units` = answers in this call (quota pre-check).

        ⚠️ The winning rung is CHECKED: an org/user key would bill someone who didn't
        opt in, invisibly to the tenant. The generic "set your own key" refusal is
        replaced by the only valid path."""
        try:
            rc = access.resolve_credential("jev", want="auto", units=units)
        except CredentialUnavailable as e:
            raise CredentialUnavailable(ErrorData(
                code=INVALID_PARAMS,
                message=("No `jev` key for your org: Jev runs on the key your TENANT "
                         "sets (a tenant admin sets it once for all its orgs). There is "
                         "no platform key, and org, team or personal keys are not "
                         "used."))) from e
        if rc.mode not in RANGS_SERVIS:
            qui = QUI_RETIRE.get(rc.mode, "whoever set it")
            raise _bad(
                f"A `jev` key set at the '{rc.mode}' level shadows the TENANT key: "
                "Jev only runs on the tenant key, so this one is not used. "
                f"To use the tenant key, {qui} must remove it (Jev connector card).")
        return JevClient(api_key=rc.key)

    def _etat_borne(state, ou: str) -> None:
        """Refuse a state over `MAX_ETAT_OCTETS` before any call."""
        taille = len(json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"))
        if taille > MAX_ETAT_OCTETS:
            raise _bad(
                f"{ou}: state is {taille} bytes, max is {MAX_ETAT_OCTETS}. Send only "
                "the fields the judgement needs, not the whole record.")

    @contextmanager
    def _upstream():
        """Turn an upstream refusal into an actionable tool error."""
        try:
            yield
        except ValueError as e:
            # Client rubric guard (unknown type, missing `criteria`); names the question.
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except (requests.ConnectionError, requests.Timeout) as e:
            raise _bad(f"Jev unreachable (network/timeout). Retry later. {e}")

    def _releve(cout_total: float) -> None:
        """Record the call's REAL spend in micro-dollars (else a 200-item batch bills as one call)."""
        u = _microdollars(cout_total)
        if u:
            session_org.note_call_trace(quantity=u)

    @mcp.tool()
    def jev_ask(state: dict, questions: dict, model: Optional[str] = None) -> dict:
        """Ask Jev typed questions about one state and get answers with probabilities.

        Jev is not a chat model: it returns a typed answer per
        question and nothing else — no prose, no justification, no tool calls. It pays
        off on BATCHES — many rows or profiles to triage with the same rubric (see
        `jev_items`). A single case you can judge yourself does not need Jev.

        ⚠️ The state is sent to a third party (OpenRouter, which passes it to TypeSafe):
        put in it only the fields the judgement needs, never a whole record or
        document.

        Put the WHOLE rubric in one call: the questions of one call are answered in
        parallel, each extra question costs about 48 input tokens, and the state is
        charged once. They cannot see each other's answers, so a question that depends
        on another's answer needs a second call.

        Returns — `{model, answers, usage}`:
            `answers[name]` is `{type: "noul", noul: 0.96}`, or
            `{type: "choice", choice, probabilities, confidence}`, or
            `{type: "score", score, probabilities, legend, confidence}`.
            `model` names the exact snapshot that answered — store it next to the
            answer, it is what makes a threshold reproducible.
            `usage` carries `input_tokens`, `output_tokens` (free) and `cost` in USD.

        A probability near 0.5 means "as likely as not", never "medium": pick two
        thresholds and leave the band between them to a human or to a stronger model.

        Args:
            state: what Jev judges — a flat or nested object (`{"title": …,
                "company": …}`), at most 16 KB of JSON. Send the fields that matter,
                not the whole record: the state is billed per token.
            questions: `{name: {type, instructions, criteria}}`.
                `noul` — criteria `{"true": …, "false": …}`, both described;
                `choice` — one entry per option, each described;
                `score` — an ORDERED list of levels, low to high.
                `criteria` is required.
            model: another model id (default: the pinned dated snapshot).
        """
        _etat_borne(state, "`state`")
        client = _client()
        with _upstream():
            r = client.decide(state, questions, model=model, timeout=LECTURE_S)
        _releve((r.get("usage") or {}).get("cost"))
        return {"model": r.get("model"), "answers": r.get("answers") or {},
                "usage": r.get("usage") or {}}

    @mcp.tool()
    def jev_items(items: list, questions: dict, model: Optional[str] = None,
                  parallel: Optional[int] = None) -> dict:
        """Ask Jev the SAME questions about many states in one call — nothing is written.

        The shape for "decide, then act": you have just read a page of rows or a list
        of profiles to triage with the same rubric, and you want a verdict per item
        before writing them, enriching them, or spending anything on them. Each item
        is judged on its own — one item's answer never depends on another's.

        ⚠️ Each state is sent to a third party (OpenRouter, which passes it to
        TypeSafe): put in it only the fields the judgement needs.

        Returns — `{model, answers, usage, failed, retry}`:
            `answers` — one entry per item, in the ORDER SENT, each
            `{index, answers: {…}}` exactly as `jev_ask` returns them, or
            `{index, error}` for an item without an answer (a state the upstream
            refused, a transient failure, or not sent in time). A failed item never
            becomes a false verdict.
            `usage` — `{decided, failed, input_tokens, cost}` for the whole call.
            `failed` — the number of items without an answer.
            `retry` — the indexes that were NOT SENT because the call ran out of time
            (about 40 s): send exactly those again. `[]` when everything was sent.

        A key problem (invalid key, no credits left) aborts the whole call rather than
        turning into N identical item errors; the answers already given are still
        billed, and the error says how many.

        Args:
            items: the states to judge, at most 200 per call and 16 KB of JSON each.
                Either the raw states, or `{"key": <your id>, "state": {…}}` to get
                your own id back beside each answer (`key` is echoed, never sent).
            questions: the rubric, same shape and same rules as `jev_ask`.
            model: another model id (default: the pinned dated snapshot).
            parallel: how many items in flight (default 10, max 50). Raise it for
                a big batch that must finish inside one call.
        """
        if not isinstance(items, list) or not items:
            raise _bad("`items`: at least one state is required.")
        if len(items) > MAX_ITEMS:
            raise _bad(f"`items`: {len(items)} states, max is {MAX_ITEMS} per call. "
                       "Split into pages.")
        try:
            fil = max(1, min(int(parallel or PARALLELE_DEFAUT), PARALLELE_MAX))
        except (TypeError, ValueError):
            raise _bad(f"`parallel`: expected an integer from 1 to {PARALLELE_MAX}, "
                       f"got {parallel!r}.")

        def _etat(i, x) -> tuple[Optional[str], dict]:
            if isinstance(x, dict) and "state" in x and isinstance(x["state"], dict):
                cle = x.get("key")
                cle, state = (str(cle) if cle is not None else None), x["state"]
            elif isinstance(x, dict):
                cle, state = None, x
            else:
                raise _bad("each `items` entry must be an object (the state) "
                           "or `{key, state}`.")
            _etat_borne(state, f"`items[{i}]`")
            return cle, state

        paires = [_etat(i, x) for i, x in enumerate(items)]
        client = _client(units=len(paires))
        # Check the rubric ONCE, not once per state.
        with _upstream():
            client.check_questions(questions)

        # Nothing starts after the window or a stop; in-flight calls finish and are billed.
        fin_depart = time.monotonic() + LOT_FENETRE_S
        arret = threading.Event()

        def _une(i: int, cle: Optional[str], state: dict) -> dict:
            if arret.is_set() or time.monotonic() >= fin_depart:
                return {"index": i, "key": cle, "_non_parti": True,
                        "error": "not sent (time budget or batch stopped) — send it again"}
            try:
                r = client.decide(state, questions, model=model, timeout=LECTURE_S)
                return {"index": i, "key": cle, "answers": r.get("answers") or {},
                        "_usage": r.get("usage") or {}, "_model": r.get("model")}
            except UpstreamHTTPError as e:
                # ⚠️ Key or balance problems stop the whole batch (not N identical errors).
                if e.status_code in (401, 402, 403):
                    raise
                return {"index": i, "key": cle, "error": _upstream_message(e)}
            except (requests.ConnectionError, requests.Timeout) as e:
                return {"index": i, "key": cle, "error": f"Jev unreachable: {e}"}

        res: list[dict] = []
        panne: Optional[Exception] = None
        try:
            with ThreadPoolExecutor(max_workers=fil) as ex:
                futurs = [ex.submit(_une, i, cle, state)
                          for i, (cle, state) in enumerate(paires)]
                for f in as_completed(futurs):
                    try:
                        res.append(f.result())
                    except Exception as e:  # noqa: SILENT — kept, re-raised below after billing
                        # Stop the batch; answers already given stay billed.
                        if panne is None:
                            panne = e
                            arret.set()
                            for autre in futurs:
                                autre.cancel()
        finally:
            # Whatever reached upstream is billed, even if the batch stops.
            cout = sum((r.get("_usage") or {}).get("cost") or 0 for r in res)
            _releve(cout)

        decides = sum(1 for r in res if "answers" in r)
        if panne is not None:
            deja = (f" {decides} answer(s) already given and billed before the stop."
                    if decides else " No answer had been given yet.")
            if isinstance(panne, UpstreamHTTPError):
                raise _bad(_upstream_message(panne) + deja) from panne
            raise panne

        res.sort(key=lambda r: r["index"])
        jetons = sum((r.get("_usage") or {}).get("input_tokens") or 0 for r in res)
        servi = next((r.get("_model") for r in res if r.get("_model")), None)
        rendus = []
        for r in res:
            ligne = {"index": r["index"]}
            if r.get("key") is not None:
                ligne["key"] = r["key"]
            if "error" in r:
                ligne["error"] = r["error"]
            else:
                ligne["answers"] = r["answers"]
            rendus.append(ligne)
        rates = sum(1 for r in rendus if "error" in r)
        return {"model": servi, "answers": rendus, "failed": rates,
                "retry": [r["index"] for r in res if r.get("_non_parti")],
                "usage": {"decided": len(rendus) - rates, "failed": rates,
                          "input_tokens": jetons, "cost": cout}}
