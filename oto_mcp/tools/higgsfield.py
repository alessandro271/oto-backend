"""Higgsfield — image and video generation across many models, each job priced
before it runs.

Wraps `oto.tools.higgsfield.HiggsfieldClient` (`Authorization: Key <id>:<secret>`),
key resolved per call via `access.resolve_api_key("higgsfield")`: the customer's own
(user or org), or the platform key when the org is granted it.

**Surface** (the `<ns>` / `<ns>_act` pair):
- `higgsfield` (read-only) — the models the key can call, the price of a job
  (`estimate`, free), the state and outputs of a job (`get_request`);
- `higgsfield_act` — submit a generation (`create_generation`), cancel a queued job, delete a
  finished job's files, get a presigned address for an input file.

**The price guard.** `create_generation` REQUIRES `max_usd`. It estimates first (free), reads a
dollar amount with `oto.tools.higgsfield.price_of`, and submits only when that amount
is known and at most `max_usd`; otherwise nothing is sent. `max_usd` itself is bounded by
the instance (`OTO_HIGGSFIELD_MAX_USD_PAR_APPEL`): above it, refused before Higgsfield is
called (`max_usd_above_cap`). Higgsfield answers some
estimates with a sentence of per-second prices instead of an amount: the amount read
from it is flagged `approximate`.

**The shared key** (closed, on grant, with a daily quota in micro-dollars of job price).
Higgsfield charges a job only once it COMPLETES — failed, nsfw and canceled jobs are free —
and a job's status carries no price. So on the platform key:
- `create_generation` checks the grant's quota for the job's price (`units`, in
  micro-dollars), submits, notes those `units`, and records the job with that price
  (`db/higgsfield_jobs.py`). It never traces a `quantity`: nothing is billed yet.
- `get_request` on a COMPLETED job, read by the org that submitted it, traces the recorded
  price as `quantity` (micro-dollars, rounded up) with the job (`higgsfield_request_id`,
  served as `job_id` by the billing lens): read N times, the biller counts it once. A
  failed, nsfw or canceled job closes its record at no cost. Read from another org, or
  when the record cannot be read, it traces 0 without the job, and the settlement
  (`higgsfield_releve.py`) bills the org that submitted it — never two lines for a job.
- Every other read traces `quantity` 0: the ledger reads a NULL quantity as 1.
A customer's own key pays Higgsfield directly: nothing is recorded, nothing billed.

**No waiting.** A submission answers at once with a `request_id`; the job runs for
seconds (a still) to minutes (a clip). Polling is `higgsfield op='get_request'`.

**An empty balance is not a dead key.** Higgsfield refuses a job it cannot pay with a
403; `HiggsfieldASec` carries a 402 into the chain so the taxonomy says
`quota_exhausted` and the key is not painted red.

Calls to the client are written out in full (`_client().get_request(…)`): this is
what makes them verifiable by the version-skew probe
(`test_tools_client_methods_exist`).
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, session_org
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import ACTE, LECTURE

logger = logging.getLogger(__name__)

_NAME = "higgsfield"

_READ_OPS = ("list_models", "estimate", "get_request")
# The end states of a job. Only `completed` is charged by Higgsfield.
TERMINAL = frozenset({"completed", "failed", "nsfw", "canceled"})
_ACT_OPS = ("create_generation", "cancel", "delete", "create_upload")

# Keys removed from the model list by default: routing internals, a schema that is
# always null, and `base_credits`, 0 on almost every model, which would read as free.
_DROP_MODELS = frozenset({"fnf_job_set_type", "is_workflow", "input_schema", "base_credits"})
_HINT = "full=true returns the raw records."

# The most ONE generation may be allowed to cost on this instance, whatever `max_usd`
# says: a slip (`max_usd=500` meant as 5) or an amount dictated by a text the agent read
# must not empty a prepaid balance in one call. A choice of the INSTANCE, read at each
# call: unset = 5 dollars, the order of one long high-resolution clip — an instance that
# needs dearer jobs raises it on purpose. Set but not a positive amount RAISES, never the
# default in silence: a typo would lift a ceiling someone meant to lower (same rule as
# `OTO_COPILOT_DAILY_CAP_USD`).
ENV_MAX_USD = "OTO_HIGGSFIELD_MAX_USD_PAR_APPEL"
MAX_USD_DEFAULT = "5"
MAX_USD_ABOVE_CAP = "max_usd_above_cap"


class HiggsfieldASec(RuntimeError):
    """The Higgsfield balance cannot pay for the job. Higgsfield says so with a 403,
    which alone would read as a refusal of access: this carries `status_code = 402` ON
    PURPOSE, so the taxonomy marks the account empty (same device as
    `elevenlabs.ElevenLabsASec`)."""
    status_code = 402


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def max_usd_cap() -> float:
    """The instance's ceiling on `max_usd`, in US dollars, read at each call."""
    raw = os.environ.get(ENV_MAX_USD, MAX_USD_DEFAULT).strip()
    try:
        cap = float(raw)
    except ValueError:
        raise RuntimeError(f"higgsfield: {ENV_MAX_USD}={raw!r} is not an amount in "
                           "US dollars") from None
    if not cap > 0 or cap == float("inf"):
        raise RuntimeError(f"higgsfield: {ENV_MAX_USD}={raw!r} must be a positive amount")
    return cap


def microdollars(usd: float) -> int:
    """A dollar price in micro-dollars, rounded UP — the unit and the rounding of Jev and
    Apify (`tools/apify.py::_microdollars`: the `round` before `ceil` keeps float noise
    from billing one micro-dollar too many)."""
    return math.ceil(round(float(usd) * 1e6, 6))


def _sur_cle_plateforme() -> bool:
    return (session_org.current_call_trace() or {}).get("key_mode") == "platform"


def _gratuit() -> None:
    """Trace the call as FREE (`quantity=0`). A read never leaves its quantity NULL: the
    ledger reads NULL as 1, and 1 µ$ per read would be billed with the jobs."""
    session_org.note_call_trace(quantity=0)


def _noter_soumission(request_id: Any, model: str, usd: float, approximate: bool) -> None:
    """Record a job submitted on the platform key, with the price checked before it left,
    so its completion bills that price (`get_request`, `higgsfield_releve.py`). A
    customer's own key pays Higgsfield directly. Never fails the submission: the job is
    already on its way, and a miss is logged."""
    if not isinstance(request_id, str) or not request_id:
        logger.warning("higgsfield: a platform-key job came back without a request_id, "
                       "not recorded")
        return
    from ..auth.hooks import current_user_sub_from_token
    from ..db import higgsfield_jobs
    try:
        sub = current_user_sub_from_token()
        higgsfield_jobs.noter(request_id, org_id=access.current_org(sub), sub=sub,
                              run_id=session_org.current_call_run(), model=model,
                              prix_microdollars=microdollars(usd), approximatif=approximate)
    except Exception:
        logger.warning("higgsfield %s: submission not recorded", request_id, exc_info=True)


def _releve(status: Any, request_id: str) -> None:
    """What a read of a job traces (see the module docstring, **The shared key**)."""
    if not _sur_cle_plateforme():
        _gratuit()
        return
    statut = status.get("status") if isinstance(status, dict) else None
    if statut not in TERMINAL:
        _gratuit()
        return
    import psycopg

    from ..db import higgsfield_jobs
    try:
        ligne = higgsfield_jobs.lire(request_id)
    except psycopg.Error:
        logger.warning("higgsfield %s: submission record unreadable, traced 0 without the "
                       "job (the settlement bills the submitter)", request_id, exc_info=True)
        _gratuit()
        return
    if ligne is None:
        # Submitted before the table, or its record was lost: no price to bill.
        _gratuit()
        return
    from ..auth.hooks import current_user_sub_from_token
    if ligne["org_id"] != access.current_org(current_user_sub_from_token()):
        _gratuit()
        return
    try:
        higgsfield_jobs.marquer(request_id, statut=statut)
    except psycopg.Error:
        logger.warning("higgsfield %s: state not recorded", request_id, exc_info=True)
    if statut != "completed":
        _gratuit()
        return
    session_org.note_call_trace(quantity=int(ligne["prix_microdollars"]),
                                higgsfield_request_id=request_id)


def _detail(e) -> str:
    """`detail` of a refusal, whatever its shape (text, validation list, object)."""
    body = e.body if isinstance(e.body, dict) else {}
    detail = body.get("detail", e.body)
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code") or detail
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail)[:400]


def _upstream_message(e) -> str:
    status, detail = e.status_code, _detail(e)
    if status == 401:
        return ("Higgsfield refused the key (HTTP 401): it is KEY_ID:KEY_SECRET from the "
                "console, joined by a colon. Retrying unchanged fails the same way.")
    if status == 403:
        return ("Higgsfield refused the call (HTTP 403): the balance is too low for this "
                "job (add funds at console.higgsfield.ai), or the key is disabled or "
                f"expired. Nothing was charged. {detail}")
    if status == 404:
        return (f"Higgsfield: not found (404, {detail}). A model slug comes from "
                "`higgsfield` op='list_models'; a request belongs to the account that made it.")
    if status == 409:
        return ("Higgsfield: the job is still queued or in progress (409); its data can be "
                "deleted once it has finished.")
    if status == 423:
        return "Higgsfield: this model is temporarily blocked (423). Retry later or use another."
    if status == 429:
        return ("Higgsfield: the account's queue is full or a limit was reached (429). Wait for "
                f"jobs to finish, then retry. {detail}")
    if status in (400, 422):
        return f"Higgsfield rejected the request (HTTP {status}): {detail}"
    if status >= 500:
        return f"Higgsfield is temporarily unavailable, or the model is not ready (HTTP {status}); retry later."
    return f"Higgsfield refused the request (HTTP {status}): {detail}"


def _refuse_ignored(op: str, **provided) -> None:
    """An argument supplied that THIS op does not use is an intent error."""
    for name, value in provided.items():
        if value is not None and value is not False:
            raise _bad(f"op='{op}' does not use `{name}`. Nothing was done.")


def _need(value, name: str, op: str):
    if value is None or value == "":
        raise _bad(f"op='{op}' requires `{name}`.")
    return value


def outputs_of(status: Any) -> list[str]:
    """Every output address of a job's status, images first, in Higgsfield's order."""
    if not isinstance(status, dict):
        return []
    urls: list[str] = []
    for key in ("images", "audios"):
        for item in status.get(key) or []:
            if isinstance(item, dict) and item.get("url") and item["url"] not in urls:
                urls.append(item["url"])
    for key in ("video", "audio", "image"):
        item = status.get(key)
        if isinstance(item, dict) and item.get("url") and item["url"] not in urls:
            urls.append(item["url"])
    return urls


def _models_view(payload: Any, full: bool, output_type: Optional[str],
                 operation_type: Optional[str], search: Optional[str]) -> Any:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return payload
    words = (search or "").lower().split()

    def keep(m: dict) -> bool:
        if output_type and m.get("output_type") != output_type:
            return False
        if operation_type and operation_type not in (m.get("operation_type") or []):
            return False
        text = " ".join(str(m.get(k) or "") for k in ("slug", "title", "description")).lower()
        return all(w in text for w in words)

    items = [m for m in items if isinstance(m, dict) and keep(m)]
    out: dict = {"total": len(items)}
    if full:
        out["items"] = items
        return out
    out["items"] = [{k: v for k, v in m.items() if k not in _DROP_MODELS} for m in items]
    out["projection"] = {"omitted": sorted(_DROP_MODELS), "hint": _HINT}
    return out


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """Probe for "test the connection": the model list, the cheapest authenticated
    call. Higgsfield has no balance endpoint: the probe says the key works, not that
    money is left (`AUTH`)."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.higgsfield import HiggsfieldClient

    try:
        HiggsfieldClient(api_key=fields.get("key")).probe()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(f"Higgsfield HTTP {e.status_code}: {e.body}")
        raise RuntimeError(f"Higgsfield HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.higgsfield import HiggsfieldClient, price_of

    connector_verify.register(_NAME, _verify)

    def _client(units: int = 1) -> HiggsfieldClient:
        """The cascade runs whole (own key > platform grant); on the platform key, the
        grant's quota is checked for `units` micro-dollars."""
        key, is_platform = access.resolve_api_key(_NAME, units=units)
        if is_platform:
            # The quota axis: a read spends none of it, a submission notes its price.
            session_org.note_call_trace(units=0)
        return HiggsfieldClient(api_key=key)

    def _run(fn, *, spends: bool = False):
        try:
            return fn()
        except UpstreamHTTPError as e:
            if spends and e.status_code == 403:
                raise _bad(_upstream_message(e)) from HiggsfieldASec(str(e))
            raise _bad(_upstream_message(e))
        except ValueError as e:
            raise _bad(str(e))

    def _priced(model: str, params: dict) -> dict:
        estimate = _run(lambda: _client().estimate(model, params))
        return {**price_of(estimate, params), "estimate": estimate}

    @mcp.tool(name="higgsfield", annotations=LECTURE)
    def higgsfield(
        op: Literal["list_models", "estimate", "get_request"],
        model: Optional[str] = None,
        params: Optional[dict] = None,
        request_id: Optional[str] = None,
        output_type: Optional[Literal["image", "video"]] = None,
        operation_type: Optional[str] = None,
        search: Optional[str] = None,
        full: bool = False,
    ) -> Any:
        """Higgsfield, read-only: its models, the price of a job, and the state of a job.
        Nothing here spends the balance.

        `op`:
        - `list_models` — the models the key can call: `slug` (what `model` takes, e.g.
          `higgsfield-ai/soul/v2/standard` for stills, `kling-video/v3.0/pro/image-to-video`,
          `bytedance/seedance-2.5/image-to-video` for clips), title, description,
          `output_type` (image, video), `operation_type` (text2image, image_edit,
          text2video, image2video, video2video…). Filter with `output_type`,
          `operation_type` and `search` (words in slug, title or description). No
          parameter schema is given: each model's page at docs.higgsfield.ai lists its
          parameters, and `estimate` names a missing or faulty one.
        - `estimate` — the price of a generation, free and with no effect: `model` and
          the same `params` as the generation. Answers `usd` (null when no amount can be
          read, `basis` says why), `approximate` (true when read from a per-second price
          sentence: it prices a 16:9 output before discount) and the raw `estimate`.
        - `get_request` — one job (`request_id`): `status` queued, in_progress,
          completed, failed (with `error`), nsfw or canceled. Once completed, `outputs`
          lists every file address and `expires_at` says when they are deleted (seven
          days after the end). Failed, nsfw and canceled jobs are not charged.

        Args:
            op: What to read (see above).
            model: `estimate`: the model slug from `list_models`.
            params: `estimate`: the model's own parameters, e.g. {"prompt": "…", "image_url": "https://…", "duration": 5, "resolution": "720p"}.
            request_id: `get_request`: the job's id, from `higgsfield_act` op='create_generation'.
            output_type: `list_models`: only image or only video models.
            operation_type: `list_models`: only models doing this, e.g. image2video.
            search: `list_models`: words that must all appear in the slug, title or description.
            full: `list_models`: the raw records instead of the tightened view.
        """
        if op not in _READ_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_READ_OPS)}.")
        if op == "list_models":
            _refuse_ignored(op, model=model, params=params, request_id=request_id)
            _gratuit()
            return _models_view(_run(lambda: _client().list_models()), full,
                                output_type, operation_type, search)
        _refuse_ignored(op, output_type=output_type, operation_type=operation_type,
                        search=search, full=full)
        if op == "estimate":
            _refuse_ignored(op, request_id=request_id)
            _gratuit()
            return _priced(_need(model, "model", op), _need(params, "params", op))
        _refuse_ignored(op, model=model, params=params)
        rid = _need(request_id, "request_id", op)
        status = _run(lambda: _client().get_request(rid))
        _releve(status, rid)
        if isinstance(status, dict) and status.get("status") == "completed":
            status["outputs"] = outputs_of(status)
        return status

    @mcp.tool(name="higgsfield_act", annotations=ACTE)
    def higgsfield_act(
        op: Literal["create_generation", "cancel", "delete", "create_upload"],
        model: Optional[str] = None,
        params: Optional[dict] = None,
        max_usd: Optional[float] = None,
        request_id: Optional[str] = None,
        content_type: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        expires_after: Optional[int] = None,
    ) -> Any:
        """Higgsfield jobs: submit one (spends the prepaid balance once it completes),
        cancel or delete one, or get an address to upload an input file.

        `op`:
        - `create_generation` — submit `model` with its `params`, REFUSED unless its price is
          known and at most `max_usd` (US dollars, required). The price is estimated
          first, free; when it cannot be read (no amount, no `duration` on a per-second
          price, a video input on a model priced without one) nothing is sent and the
          answer says why. Answers at once: `request_id`, `status` queued, the `price`
          used for the check. The job runs for seconds (a still) to minutes (a clip):
          follow it with `higgsfield` op='get_request'. Never retried; sending the same
          `idempotency_key` again returns the original job instead of a second one.
          Many models make sound by default (`generate_audio`, `sound`): turn it off in
          `params` unless sound is wanted, it costs more.
        - `cancel` — cancel a job still queued (`request_id`); it is refunded. A job in
          progress can no longer be canceled.
        - `delete` — delete a finished job's files, prompt and parameters now
          (`request_id`). Irreversible; billing records stay.
        - `create_upload` — a presigned address for one input file (`content_type`:
          image/jpeg, image/png, image/webp, image/gif, audio/wav, video/mp4). PUT the
          bytes to `upload_url` within the hour with EVERY header of `upload_headers`
          (never the API key), e.g. `curl -X PUT --upload-file still.png -H
          "Content-Type: image/png" -H "x-amz-tagging: retention=temporary" "<upload_url>"`,
          then pass `public_url` in `params` (`image_url`, `video_url`…). A file that is
          already at a public https address needs no upload.

        Output links (images, videos) are the provider's PUBLIC addresses, readable by
        anyone holding the link for seven days (`expires_after` shortens it, 3600 seconds
        or more): download them right away, never paste them anywhere public.

        Args:
            op: What to do (see above).
            model: `create_generation`: the model slug from `higgsfield` op='list_models'.
            params: `create_generation`: the model's own parameters, e.g. {"prompt": "…", "image_url": "https://…", "duration": 5, "resolution": "720p"}.
            max_usd: `create_generation`: the most this job may cost, in US dollars, at most the instance's ceiling (refused above it); above `max_usd`, or when the price cannot be read, nothing is sent.
            request_id: `cancel`, `delete`: the job's id.
            content_type: `create_upload`: the input file's type.
            idempotency_key: `create_generation`: your own key (1-255 visible ASCII characters) to make a repeat safe; a fresh one otherwise.
            expires_after: `create_generation`: seconds to keep the outputs after the end, 3600 or more (default: seven days).
        """
        if op not in _ACT_OPS:
            raise _bad(f"Unknown op {op!r}: expected one of {', '.join(_ACT_OPS)}. "
                       "Nothing was done.")
        if op == "create_generation":
            _refuse_ignored(op, request_id=request_id, content_type=content_type)
            m, p = _need(model, "model", op), _need(params, "params", op)
            cap = _need(max_usd, "max_usd", op)
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
                raise _bad("`max_usd` must be a positive amount in US dollars. Nothing was sent.")
            ceiling = max_usd_cap()
            if cap > ceiling:
                raise _bad(f"{MAX_USD_ABOVE_CAP}: max_usd ${cap:.2f} is above this instance's "
                           f"ceiling of ${ceiling:.2f} per generation. Nothing was sent; split "
                           "the work into cheaper jobs, or ask the instance's operator.")
            price = _priced(m, p)
            usd = price["usd"]
            if usd is None:
                raise _bad(f"Higgsfield: the price of this job cannot be read ({price['basis']}), "
                           "so it was not sent. Nothing was charged.")
            if usd > cap:
                raise _bad(f"Higgsfield: this job costs about ${usd:.3f} ({price['basis']}), above "
                           f"max_usd ${cap:.2f}. Nothing was sent; raise max_usd only once that "
                           "price is agreed.")
            # Resolved again for the job's price: on the platform key, the grant's quota is
            # checked for it BEFORE anything is sent.
            units = microdollars(usd)
            client = _client(units=units)
            platform = _sur_cle_plateforme()
            answer = _run(lambda: client.generate(m, p, idempotency_key=idempotency_key,
                                                  expires_after=expires_after), spends=True)
            if platform:
                session_org.note_call_trace(units=units)
                _noter_soumission(answer.get("request_id") if isinstance(answer, dict)
                                  else None, m, usd, bool(price["approximate"]))
            if isinstance(answer, dict):
                answer["price"] = {k: price[k] for k in ("usd", "approximate", "basis")}
            return answer
        _refuse_ignored(op, model=model, params=params, max_usd=max_usd,
                        idempotency_key=idempotency_key, expires_after=expires_after)
        if op == "create_upload":
            _refuse_ignored(op, request_id=request_id)
            ct = _need(content_type, "content_type", op)
            return _run(lambda: _client().create_upload_url(ct))
        _refuse_ignored(op, content_type=content_type)
        rid = _need(request_id, "request_id", op)
        if op == "cancel":
            return _run(lambda: _client().cancel_request(rid))
        return _run(lambda: _client().delete_request(rid))
