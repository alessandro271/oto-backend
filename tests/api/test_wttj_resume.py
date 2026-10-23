"""`GET /api/wttj/resume/{token}` — the DURABLE link to a WTTJ candidate's resume.

A PUBLIC route (no JWT, the path token is the authority): these tests play it end to
end through a real HTTP client, without a database, like the CI — membership, team,
suspension, activation, vault, journal and the lib's client are stubbed. What they
hold:

- only a resume token (signed, unexpired, of ITS `typ`) opens the route, and it
  opens AGAIN (not single use);
- each opening redirects (302, never cached) to the `resume_url` WTTJ signs NOW,
  for the SEALED candidate;
- the sealed org and team are REPLAYED: the key is resolved under them (org level
  forced when no team is sealed), and an account out of the org or out of the team
  holds a dead link — refused before any WTTJ call;
- every refusal is named, short, and carries no internal id;
- each opening past the signature leaves a journal row, without the token or the URL.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

KEY = "wttj-key-secret-123"
SUB = "u-recruiter"
ORG = 7
GROUP = 31
REF = "cand-ref-1"
FRESH = "https://cdn.example.test/resume.pdf?Expires=1&Signature=s"


@pytest.fixture
def bench(monkeypatch):
    import oto.tools.wttj_ats as lib
    from oto_mcp import (access, db, garde_identite, group_store, org_suspension, roles,
                         session_org)
    from oto_mcp.api import wttj as route
    from oto_mcp.connectors import activation

    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "bench-secret-long-enough")
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://oto.example.test")

    state = {"journal": [], "members": {(SUB, ORG)}, "teams": {(SUB, GROUP)},
             "resolutions": [], "candidate": {"reference": REF, "resume_url": FRESH},
             "error": None, "suspended": False, "cut": None, "key_error": None,
             "identity": None}

    def _resolve(provider, *a, sub=None, **k):
        state["resolutions"].append({
            "provider": provider, "sub": sub, "org": session_org.current_call_org(),
            "group": session_org.current_call_group(),
            "view_group": session_org.current_view_group()})
        if state["key_error"] is not None:
            raise state["key_error"]
        rc = MagicMock()
        rc.key, rc.mode = KEY, "org"
        return rc

    def _get_candidate(ref, **kw):
        state.setdefault("asked", []).append(ref)
        if state["error"] is not None:
            raise state["error"]
        return state["candidate"]

    inst = MagicMock()
    inst.get_candidate.side_effect = _get_candidate
    built = []

    def _client(api_key=None):
        built.append(api_key)
        return inst

    monkeypatch.setattr(lib, "WttjAtsClient", _client)
    monkeypatch.setattr(db, "insert_tool_call", state["journal"].append)
    monkeypatch.setattr(garde_identite, "refus", lambda sub: state["identity"])
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: (sub, org) in state["members"])
    monkeypatch.setattr(group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": ORG} if gid == GROUP else None)
    monkeypatch.setattr(group_store, "is_group_member",
                        lambda sub, gid: (sub, gid) in state["teams"])
    monkeypatch.setattr(org_suspension, "refus",
                        lambda org: "suspended" if state["suspended"] else None)
    monkeypatch.setattr(activation, "cran_qui_coupe", lambda *a: state["cut"])
    monkeypatch.setattr(access, "resolve_credential", _resolve)

    app = Starlette(routes=[Route("/api/wttj/resume/{token}", route.resume_open,
                                  methods=["GET"])])
    state["http"] = TestClient(app, follow_redirects=False)
    state["built"] = built
    return state


def _link(group=GROUP, ref=REF, org=ORG) -> str:
    from oto_mcp.tools import wttj_resume
    return wttj_resume.mint(SUB, org, group, ref).replace("https://oto.example.test", "")


def _open(bench, path):
    return bench["http"].get(path)


def _journal(bench, n=1, delay=2.0):
    end = time.monotonic() + delay
    while len(bench["journal"]) < n and time.monotonic() < end:
        time.sleep(0.01)
    return bench["journal"]


def _assert_no_internal_id(r):
    body = r.text
    for value in (str(ORG), str(GROUP), SUB, KEY):
        assert value not in body, f"{value!r} leaks into the refusal page"


# ── The nominal path ─────────────────────────────────────────────────────────────

def test_an_opening_redirects_to_the_freshly_signed_resume(bench):
    r = _open(bench, _link())
    assert r.status_code == 302, r.text
    assert r.headers["location"] == FRESH
    assert r.headers["cache-control"] == "no-store", (
        "a cached redirect would outlive WTTJ's 30-minute signature")
    assert bench["asked"] == [REF], "the candidate comes from the token, never the client"
    assert bench["built"] == [KEY], "the client is built with the key just resolved"


def test_the_link_opens_again(bench):
    path = _link()
    assert _open(bench, path).status_code == 302
    assert _open(bench, path).status_code == 302, "not single use: the link reopens"
    assert bench["asked"] == [REF, REF], "WTTJ is asked again at each click"


def test_the_key_is_resolved_under_the_SEALED_org_and_team(bench):
    _open(bench, _link())
    res, = bench["resolutions"]
    assert (res["provider"], res["sub"], res["org"], res["group"]) == (
        "wttj", SUB, ORG, GROUP), (
        "without the pin, the cascade would fall back on the account's home org/team")


def test_without_a_sealed_team_the_org_level_is_forced(bench):
    """A link minted at the org level must not pick up the account's home team at
    replay: the team axis is forced to the org level (`X-Oto-Group: 0`)."""
    _open(bench, _link(group=None))
    res, = bench["resolutions"]
    assert res["group"] is None and res["view_group"] == 0 and res["org"] == ORG


def test_the_pins_do_not_survive_the_opening(bench):
    from oto_mcp import session_org
    _open(bench, _link())
    _open(bench, _link(group=None))
    assert session_org.current_call_org() is None
    assert session_org.current_call_group() is None
    assert session_org.current_view_group() is None


def test_the_journal_keeps_the_opening_without_token_or_url(bench):
    path = _link()
    _open(bench, path)
    row, = _journal(bench)
    assert (row["tool"], row["sub"], row["org_id"], row["ok"], row["key_mode"]) == (
        "wttj_resume_link", SUB, ORG, True, "org")
    assert row["args"] == {"candidate_reference": REF, "group_id": GROUP}
    flat = repr(row)
    assert path.rsplit("/", 1)[-1] not in flat and FRESH not in flat and KEY not in flat


# ── The token ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/wttj/resume/garbage",
                                  "/api/wttj/resume/a.b"])
def test_an_unreadable_token_is_refused(bench, path):
    r = _open(bench, path)
    assert r.status_code == 401 and r.json()["error"] == "invalid_or_expired_token"
    assert bench["resolutions"] == [] and bench["journal"] == []


def test_a_token_of_another_family_is_refused(bench):
    from oto_mcp import upload_tokens
    tok, _ = upload_tokens.sign(SUB, ORG, {"kind": "wttj_resume", "group": None,
                                           "candidate_reference": REF}, typ="relay")
    r = _open(bench, f"/api/wttj/resume/{tok}")
    assert r.status_code == 401 and r.json()["error"] == "invalid_or_expired_token"


def test_an_expired_token_is_refused(bench):
    from oto_mcp import upload_tokens
    from oto_mcp.tools import wttj_resume
    tok, _ = upload_tokens.sign(SUB, ORG, {"kind": wttj_resume.KIND, "group": None,
                                           "candidate_reference": REF},
                                ttl=-10, typ=wttj_resume.TYP)
    r = _open(bench, f"/api/wttj/resume/{tok}")
    assert r.status_code == 401 and r.json()["error"] == "invalid_or_expired_token"


def test_the_token_lives_thirty_days():
    from oto_mcp.tools import wttj_resume
    assert wttj_resume.TTL_S == 30 * 24 * 3600


# ── The replay ───────────────────────────────────────────────────────────────────

def test_an_account_out_of_the_org_holds_a_dead_link(bench):
    bench["members"].clear()
    r = _open(bench, _link())
    assert r.status_code == 403 and r.json()["error"] == "not_an_org_member"
    assert bench["resolutions"] == [] and "asked" not in bench
    _assert_no_internal_id(r)


def test_a_member_removed_from_the_team_holds_a_dead_link(bench):
    bench["teams"].clear()
    r = _open(bench, _link())
    assert r.status_code == 403 and r.json()["error"] == "not_a_team_member"
    assert bench["resolutions"] == [] and "asked" not in bench
    _assert_no_internal_id(r)


def test_a_team_of_another_org_is_refused(bench):
    r = _open(bench, _link(group=999))
    assert r.status_code == 403 and r.json()["error"] == "not_a_team_member"


def test_a_link_without_org_is_refused(bench):
    r = _open(bench, _link(org=None))
    assert r.status_code == 403 and r.json()["error"] == "no_organization"


def test_a_suspended_org_is_refused(bench):
    bench["suspended"] = True
    r = _open(bench, _link())
    assert r.status_code == 403 and r.json()["error"] == "org_suspended"
    assert "asked" not in bench
    _assert_no_internal_id(r)


def test_a_disabled_connector_is_refused(bench):
    bench["cut"] = "org"
    r = _open(bench, _link())
    assert r.status_code == 403 and r.json()["error"] == "connector_disabled"
    assert "asked" not in bench


def test_a_suspended_account_is_refused_by_the_identity_guard(bench):
    bench["identity"] = ("account_suspended", "paused since a date, by someone")
    r = _open(bench, _link())
    assert r.status_code == 403 and r.json()["error"] == "account_suspended"
    assert "someone" not in r.text, "the guard's own sentence is not served here"
    assert bench["resolutions"] == []


def test_a_key_no_longer_reachable_is_named(bench):
    from mcp.types import ErrorData, INVALID_PARAMS
    from oto_mcp.mcp_errors import McpError
    bench["key_error"] = McpError(ErrorData(code=INVALID_PARAMS,
                                            message=f"no key for org {ORG}"))
    r = _open(bench, _link())
    assert r.status_code == 424 and r.json()["error"] == "credential_unavailable"
    assert "asked" not in bench
    _assert_no_internal_id(r)


# ── WTTJ's answer ────────────────────────────────────────────────────────────────

def test_a_candidate_without_resume_is_a_named_404(bench):
    bench["candidate"] = {"reference": REF, "resume_url": None}
    r = _open(bench, _link())
    assert r.status_code == 404 and r.json()["error"] == "no_resume"
    row, = _journal(bench)
    assert row["ok"] is False and row["error"] == "no_resume"


@pytest.mark.parametrize("status, code, served", [
    (404, "candidate_not_found", 404),
    (401, "wttj_access_refused", 424),
    (403, "wttj_access_refused", 424),
    (429, "wttj_rate_limited", 503),
    (500, "wttj_unavailable", 502),
])
def test_an_upstream_refusal_is_named(bench, status, code, served):
    from oto.tools.common.errors import UpstreamHTTPError
    bench["error"] = UpstreamHTTPError(status, {"error": "x"}, service="wttj")
    r = _open(bench, _link())
    assert r.status_code == served and r.json()["error"] == code
    _assert_no_internal_id(r)


def test_an_unreachable_wttj_is_named(bench):
    import requests
    bench["error"] = requests.ConnectionError("boom")
    r = _open(bench, _link())
    assert r.status_code == 502 and r.json()["error"] == "wttj_unreachable"


def test_never_redirects_to_a_non_https_address(bench):
    bench["candidate"] = {"reference": REF, "resume_url": "javascript:alert(1)"}
    r = _open(bench, _link())
    assert r.status_code == 502 and r.json()["error"] == "wttj_unavailable"


# ── The route is mounted, declared and covered ───────────────────────────────────

def test_the_route_is_served_by_the_real_table():
    from oto_mcp.api import routes as api_routes
    from oto_mcp.api import wttj as route
    served = [r for r in api_routes.make_routes(object())
              if r.path == "/api/wttj/resume/{token}"]
    assert [r.endpoint for r in served] == [route.resume_open]
    assert served[0].methods >= {"GET"}


def test_the_token_in_the_path_is_masked_in_the_journal():
    from oto_mcp import journal_secrets
    from oto_mcp.api import routes as api_routes
    api_routes.make_routes(object())
    reduced, masks = journal_secrets.route_and_secrets("/api/wttj/resume/abc.def")
    assert "abc.def" not in reduced and masks and "abc.def" not in repr(masks)
