"""A passing 502 and a connection that does not go through are retried ONCE, inside a
budget for the whole call.

Measured 2026-11-21: a FOD search answered 502 (a gateway HTML page, served as is in
the message) in about a second and passed on the next call. The client only retried
503 and 429. But a 502 can also cost ~30 s (the upstream that says nothing): retrying
that one would double the call past the ~60 s where an MCP client hangs up. So the
retry happens only while the whole call stays inside `_DUREE_MAX_REPRISE_S`, and its
read is bounded by what is left. 504 and read timeouts are still not retried.
"""
from __future__ import annotations

import httpx
import pytest

_PAGE_PASSERELLE = (
    "<!DOCTYPE html>\n<html lang=\"en-US\"><head>\n"
    "<title>upstream.example | 502: Bad gateway</title></head><body>…</body></html>")


class _Reponse:
    def __init__(self, code, texte=None):
        self.status_code = code
        self.headers = {}
        self.text = texte if texte is not None else "{}"
        self.request = httpx.Request("GET", "http://10.0.0.1:8000/api/x")

    def json(self):
        if self.text.startswith("<"):
            raise ValueError("not JSON")
        return {"ok": True} if self.status_code < 400 else {"detail": "x"}


class _Client:
    """Serves the given responses (or exceptions) in order, then 200. Records the
    timeout of each call, and can advance the clock by `couts[i]` seconds."""

    def __init__(self, *suite, horloge=None, couts=()):
        self.suite, self.appels, self.timeouts = list(suite), 0, []
        self.horloge, self.couts = horloge, list(couts)

    def request(self, method, path, **kw):
        self.appels += 1
        self.timeouts.append(kw.get("timeout"))
        if self.horloge is not None and self.couts:
            self.horloge[0] += self.couts.pop(0)
        nxt = self.suite.pop(0) if self.suite else _Reponse(200)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def fod(monkeypatch):
    from oto_mcp.fod import http as F

    monkeypatch.setattr(F, "_RETRY_BACKOFF_S", 0.001)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    return F


@pytest.fixture
def horloge(fod, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(fod.time, "monotonic", lambda: t[0])
    return t


def test_a_fast_502_is_retried(fod, monkeypatch, horloge):
    cli = _Client(_Reponse(502, _PAGE_PASSERELLE), horloge=horloge, couts=[1, 1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    assert fod.get("/x") == {"ok": True}
    assert cli.appels == 2


def test_the_retry_read_is_bounded_by_what_is_left(fod, monkeypatch, horloge):
    cli = _Client(_Reponse(502, _PAGE_PASSERELLE), horloge=horloge, couts=[5, 1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    fod.get("/x")
    lecture = cli.timeouts[1].read
    assert lecture <= fod._DUREE_MAX_REPRISE_S - 5
    assert lecture >= fod._RESTE_MIN_S


def test_a_slow_502_is_not_retried(fod, monkeypatch, horloge):
    """A 502 that already cost 40 s leaves no room for a second attempt."""
    cli = _Client(_Reponse(502, _PAGE_PASSERELLE), horloge=horloge, couts=[40])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(fod.FodError):
        fod.get("/x")
    assert cli.appels == 1


def test_a_lasting_502_is_retried_once_and_named(fod, monkeypatch, horloge):
    cli = _Client(*[_Reponse(502, _PAGE_PASSERELLE) for _ in range(5)],
                  horloge=horloge, couts=[1, 1, 1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(fod.FodError) as e:
        fod.get("/x")
    assert cli.appels == 2
    msg = str(e.value)
    assert "Source amont indisponible" in msg and "RÉESSAYABLE" in msg
    assert "502: Bad gateway" in msg, "the page's <title> says what matters"
    assert "<!DOCTYPE" not in msg and "<html" not in msg, "no markup served"
    assert "10.0.0.1" not in msg


def test_a_504_is_still_not_retried(fod, monkeypatch, horloge):
    cli = _Client(_Reponse(504), horloge=horloge, couts=[1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(fod.FodError):
        fod.get("/x")
    assert cli.appels == 1


def test_a_connect_timeout_is_retried_once(fod, monkeypatch, horloge):
    cli = _Client(httpx.ConnectTimeout("t"), horloge=horloge, couts=[5, 1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    assert fod.get("/x") == {"ok": True}
    assert cli.appels == 2


def test_a_dead_connection_is_named_retryable(fod, monkeypatch, horloge):
    cli = _Client(httpx.ConnectTimeout("t"), httpx.ConnectTimeout("t"),
                  horloge=horloge, couts=[5, 5])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(RuntimeError, match="RÉESSAYABLE"):
        fod.get("/x")
    assert cli.appels == 2


def test_a_read_timeout_is_not_retried(fod, monkeypatch, horloge):
    cli = _Client(httpx.ReadTimeout("t"), horloge=horloge, couts=[1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(RuntimeError, match="pas de réponse"):
        fod.get("/x")
    assert cli.appels == 1


def test_no_transient_retry_means_the_502_goes_back_as_is(fod, monkeypatch, horloge):
    monkeypatch.setattr(fod, "_REPRISES_TRANSITOIRES", 0)
    cli = _Client(_Reponse(502, _PAGE_PASSERELLE), horloge=horloge, couts=[1])
    monkeypatch.setattr(fod, "_c", lambda: cli)
    with pytest.raises(fod.FodError):
        fod.get("/x")
    assert cli.appels == 1
