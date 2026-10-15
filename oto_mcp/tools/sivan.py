"""Sivan tools — staff scheduling and workforce management (read-only).

Wraps `oto.tools.sivan.SivanClient` (REST, `X-Api-Key` header). Six read-only
tools, one per endpoint of the group's data:

- `sivan_companies` — the group's enabled companies (with employees, contracts,
  locations);
- `sivan_salaries` — the group's employees, with optional hour/absence counters;
- `sivan_events` — the PLANNED schedule over a window (≤ 31 days);
- `sivan_badged_events` — clock-in/out punches over a window (≤ 31 days,
  requires the badging module);
- `sivan_absences` — absences over a window (no 31-day cap);
- `sivan_timesheets` — worked-time aggregates per employee/contract (≤ 31 days).

⚠️ The key is a GROUP key: it reads every company of the group, not one in
particular. Durations are in SECONDS.

Client calls are written out (`_client().list_companies(…)`) so the version-skew
probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    """Turn a Sivan refusal into an actionable message, by status."""
    status = e.status_code
    detail = e.body if isinstance(e.body, (str, dict)) else ""
    if status == 401:
        return ("Sivan rejected the key (401) — check the group API key set on this "
                "connector (an administrator generates it at the GROUP level in "
                "Sivan; it is sent as the `X-Api-Key` header).")
    if status == 403:
        return ("Sivan refused the operation (403) — the group key does not allow it, "
                "or the required module is not enabled (badged events need the badging "
                f"module): {detail}")
    if status == 404:
        return f"Sivan: not found (404) — {detail}."
    if status == 429:
        return "Sivan: too many requests (429) — retry later."
    if status in (500, 502, 503, 504):
        return f"Sivan is temporarily unavailable (HTTP {status}) — retry later."
    return f"Sivan refused the request (HTTP {status}): {detail}"


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001
    """"Test connection" probe: lists the group's companies (`list_companies`) —
    a read with no side effect, a clean 401 if the key is wrong."""
    from oto.tools.sivan import SivanClient
    SivanClient(api_key=fields["key"]).list_companies()


def register(mcp: FastMCP) -> None:
    from oto.tools.sivan import SivanClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register("sivan", _verify)

    def _client() -> SivanClient:
        key, _ = access.resolve_api_key("sivan")
        return SivanClient(api_key=key)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # --- companies & employees -------------------------------------------------

    @mcp.tool()
    def sivan_companies() -> Any:
        """Sivan — the group's enabled companies.

        Each company comes with its employees, contracts and locations. The key
        is a GROUP key, so this returns every company of the group.
        """
        return _run(lambda: _client().list_companies())

    @mcp.tool()
    def sivan_salaries(
        with_counter: bool = False,
        with_absences_counter: bool = False,
    ) -> Any:
        """Sivan — the group's employees (salariés).

        Args:
            with_counter: also return each employee's hours counter.
            with_absences_counter: also return each employee's absences counter.
        """
        return _run(lambda: _client().list_salaries(
            with_counter=with_counter, with_absences_counter=with_absences_counter))

    # --- schedule & punches ----------------------------------------------------

    @mcp.tool()
    def sivan_events(
        start: str,
        end: str,
        with_absence_type: bool = False,
        companies_ids: Optional[List[int]] = None,
        salaries_ids: Optional[List[int]] = None,
    ) -> Any:
        """Sivan — the PLANNED schedule over a window, grouped by company then employee.

        ⚠️ The window (`start`..`end`) must be at most 31 days. Durations are in
        seconds.

        Args:
            start: window start (ISO date/datetime).
            end: window end (ISO date/datetime). At most 31 days after `start`.
            with_absence_type: also expand the type of each planned absence.
            companies_ids: restrict to these company ids (default: the whole group).
            salaries_ids: restrict to these employee ids.
        """
        return _run(lambda: _client().list_events(
            start, end, with_absence_type=with_absence_type,
            companies_ids=companies_ids, salaries_ids=salaries_ids))

    @mcp.tool()
    def sivan_badged_events(
        start: str,
        end: str,
        companies_ids: Optional[List[int]] = None,
        salaries_ids: Optional[List[int]] = None,
    ) -> Any:
        """Sivan — clock-in/out punches (pointages) over a window.

        ⚠️ Requires the badging module (badgeuse) on the Sivan side; without it
        Sivan refuses (403). The window (`start`..`end`) must be at most 31 days.
        Durations are in seconds.

        Args:
            start: window start (ISO date/datetime).
            end: window end (ISO date/datetime). At most 31 days after `start`.
            companies_ids: restrict to these company ids (default: the whole group).
            salaries_ids: restrict to these employee ids.
        """
        return _run(lambda: _client().list_badged_events(
            start, end, companies_ids=companies_ids, salaries_ids=salaries_ids))

    # --- absences --------------------------------------------------------------

    @mcp.tool()
    def sivan_absences(
        start: str,
        end: str,
        status: Optional[Literal["PENDING", "ACCEPTED", "REFUSED"]] = None,
        salaries_ids: Optional[List[int]] = None,
    ) -> Any:
        """Sivan — absences over a window.

        Unlike the schedule and punches, there is NO 31-day cap on this window.

        Args:
            start: window start (ISO date/datetime).
            end: window end (ISO date/datetime).
            status: keep only absences in this request state (PENDING, ACCEPTED
                or REFUSED). Default: every status.
            salaries_ids: restrict to these employee ids.
        """
        return _run(lambda: _client().list_absences(
            start, end, status=status, salaries_ids=salaries_ids))

    # --- worked-time aggregates ------------------------------------------------

    @mcp.tool()
    def sivan_timesheets(
        start: str,
        end: str,
        salaries_ids: Optional[List[int]] = None,
    ) -> Any:
        """Sivan — worked-time aggregates per employee and contract over a window.

        ⚠️ The window (`start`..`end`) must be at most 31 days. Durations are in
        seconds.

        Args:
            start: window start (ISO date/datetime).
            end: window end (ISO date/datetime). At most 31 days after `start`.
            salaries_ids: restrict to these employee ids (default: the whole group).
        """
        return _run(lambda: _client().list_timesheets(
            start, end, salaries_ids=salaries_ids))
