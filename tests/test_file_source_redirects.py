"""Opt-in redirects for a fetched file: each hop re-checked against the egress guard,
no https→http, a hop cap. Default callers still refuse any redirect."""
from __future__ import annotations

import httpx
import pytest

from oto_mcp import egress
from oto_mcp import file_source as fs

PUBLIC = {"93.184.216.34"}


@pytest.fixture(autouse=True)
def reseau(monkeypatch):
    monkeypatch.setattr(egress, "resolved_addresses",
                        lambda hote, port: {"10.0.0.5"} if hote == "internal.test" else PUBLIC)
    monkeypatch.setattr(fs.url_perimeter, "perimeter_of_call", lambda: None)


def _serve(monkeypatch, routes: dict):
    def handler(request):
        rep = routes[str(request.url)]
        return rep if isinstance(rep, httpx.Response) else httpx.Response(200, content=rep)
    vrai = httpx.Client

    class Client(vrai):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, "Client", Client)


def _go(url):
    return httpx.Response(302, headers={"location": url})


def test_a_redirect_is_followed_when_asked(monkeypatch):
    _serve(monkeypatch, {"https://a.example/f": _go("https://cdn.example/f.csv"),
                         "https://cdn.example/f.csv": b"email\nx@y\n"})
    rf = fs.resolve({"kind": "url", "url": "https://a.example/f"}, follow_redirects=True)
    assert rf.data == b"email\nx@y\n" and rf.filename == "f.csv"


def test_default_callers_still_refuse_redirects(monkeypatch):
    _serve(monkeypatch, {"https://a.example/f": _go("https://cdn.example/f.csv")})
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": "https://a.example/f"})


def test_a_hop_to_a_private_address_is_refused(monkeypatch):
    _serve(monkeypatch, {"https://a.example/f": _go("https://internal.test/admin")})
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": "https://a.example/f"}, follow_redirects=True)


def test_https_to_http_is_refused(monkeypatch):
    _serve(monkeypatch, {"https://a.example/f": _go("http://b.example/f")})
    with pytest.raises(fs.FileSourceError, match="https to http"):
        fs.resolve({"kind": "url", "url": "https://a.example/f"}, follow_redirects=True)


def test_more_than_five_hops_is_refused(monkeypatch):
    routes = {f"https://a.example/{i}": _go(f"https://a.example/{i + 1}") for i in range(7)}
    _serve(monkeypatch, routes)
    with pytest.raises(fs.FileSourceError, match="redirects"):
        fs.resolve({"kind": "url", "url": "https://a.example/0"}, follow_redirects=True)


def test_the_size_cap_holds_after_a_redirect(monkeypatch):
    _serve(monkeypatch, {"https://a.example/f": _go("https://cdn.example/big"),
                         "https://cdn.example/big": b"x" * 100})
    with pytest.raises(fs.FileSourceError):
        fs.resolve({"kind": "url", "url": "https://a.example/f"}, max_bytes=10,
                   follow_redirects=True)


def test_a_native_drive_sheet_exports_as_csv(monkeypatch):
    import sys
    import types

    class FakeDrive:
        def __init__(self, **kw): pass
        def get_file_metadata(self, file_id):
            return {"mimeType": "application/vnd.google-apps.spreadsheet"}
        def export_file_bytes(self, file_id, mime):
            assert mime == "text/csv"
            return {"filename": "Leads", "data": b"email\nx@y\n"}
    mod = types.ModuleType("oto.tools.google.drive.lib.drive_client")
    mod.DriveClient = FakeDrive
    monkeypatch.setitem(sys.modules, "oto.tools.google.drive.lib.drive_client", mod)
    monkeypatch.setattr(fs, "_google_creds", lambda account, service: object())
    rf = fs.resolve({"kind": "drive", "file_id": "1x", "export_sheets": True})
    assert rf.data == b"email\nx@y\n" and rf.filename == "Leads.csv" and rf.mime == "text/csv"


# --- échéance TOTALE du téléchargement -----------------------------------------

def _horloge(monkeypatch, pas: float = 1.0):
    t = {"now": 1000.0}

    def monotonic():
        t["now"] += pas
        return t["now"]
    monkeypatch.setattr(fs.time, "monotonic", monotonic)
    return t


def test_a_dripping_server_is_cut_at_the_deadline(monkeypatch):
    """Un octet à la fois : le timeout par phase de httpx ne borne pas le TOTAL."""
    def goutte():
        for _ in range(1000):
            yield b"a"
    _serve(monkeypatch, {"https://a.example/f.csv": httpx.Response(200, content=goutte())})
    t = _horloge(monkeypatch)
    with pytest.raises(fs.FileSourceError, match="budget"):
        fs.resolve({"kind": "url", "url": "https://a.example/f.csv"},
                   follow_redirects=True, deadline=t["now"] + 20)


def test_a_past_deadline_fetches_nothing(monkeypatch):
    vus = []

    def handler(request):
        vus.append(str(request.url))
        return httpx.Response(200, content=b"x")
    vrai = httpx.Client

    class Client(vrai):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, "Client", Client)
    t = _horloge(monkeypatch)
    with pytest.raises(fs.FileSourceError, match="budget"):
        fs.resolve({"kind": "url", "url": "https://a.example/f.csv"},
                   follow_redirects=True, deadline=t["now"] - 1)
    assert vus == []


def test_without_deadline_behaviour_is_unchanged(monkeypatch):
    _serve(monkeypatch, {"https://a.example/f.csv": b"email\nx@y\n"})
    rf = fs.resolve({"kind": "url", "url": "https://a.example/f.csv"})
    assert rf.data == b"email\nx@y\n"
