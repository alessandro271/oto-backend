"""`oto_import`: the server fetches the file. Source rewriting, format, refusals and
the resume guard — the store and the fetch are stubbed; the real write is in
`tests/datastore/test_oto_import_db.py`."""
from __future__ import annotations

import pytest

from oto_mcp import file_source, upload_tokens
from oto_mcp.capabilities import uploads as up
from oto_mcp.capabilities._types import AuthzDenied


def _rf(data=b"email\nx@y\n", name="leads.csv", mime="text/csv"):
    return file_source.ResolvedFile(data, name, mime)


@pytest.mark.parametrize("link, export", [
    ("https://docs.google.com/spreadsheets/d/AbC_1-x/edit#gid=42",
     "https://docs.google.com/spreadsheets/d/AbC_1-x/export?format=csv&gid=42"),
    ("https://docs.google.com/spreadsheets/d/AbC/view?usp=sharing",
     "https://docs.google.com/spreadsheets/d/AbC/export?format=csv"),
])
def test_a_public_sheet_link_becomes_its_csv_export(link, export):
    assert up._source_for_fetch({"kind": "url", "url": link})["url"] == export


def test_other_links_are_untouched_and_drive_asks_for_the_sheet_export():
    url = "https://files.example.com/a.csv?sig=secret"
    assert up._source_for_fetch({"kind": "url", "url": url})["url"] == url
    assert up._source_for_fetch({"kind": "drive", "file_id": "x"})["export_sheets"]


def test_the_receipt_url_drops_the_query_string():
    assert up._public_url("https://s3.example.com/b/f.csv?X-Amz-Signature=abc") == \
        "https://s3.example.com/b/f.csv"


def test_the_log_masks_the_source_url():
    from oto_mcp.calllog import truncated_args
    logged = truncated_args({"source": {"kind": "url",
                                        "url": "https://x.example/f.csv?token=s3cr3t"}},
                            tool="oto_import")
    assert "s3cr3t" not in str(logged)


@pytest.mark.parametrize("rf, fmt", [
    (_rf(name="a.csv", mime="application/octet-stream"), "csv"),
    (_rf(name="a.tsv", mime="application/octet-stream"), "csv"),
    (_rf(name="a.jsonl", mime="application/octet-stream"), "ndjson"),
    (_rf(name="export", mime="text/csv"), "csv"),
])
def test_the_format_comes_from_the_name_or_the_type(rf, fmt):
    assert up._format_of(rf, None) == fmt


def test_an_unknown_format_is_refused_by_name():
    with pytest.raises(AuthzDenied) as e:
        up._format_of(_rf(name="a.bin", mime="application/octet-stream"), None)
    assert e.value.code == "unknown_format"


def test_a_web_page_is_refused_and_a_private_sheet_says_how_to_fix_it():
    page = _rf(data=b"<!DOCTYPE html><html>login</html>", mime="text/html")
    with pytest.raises(AuthzDenied) as e:
        up._refuse_html(page, {"kind": "url",
                               "url": "https://docs.google.com/spreadsheets/d/X/edit"})
    assert e.value.code == "not_a_data_file" and "anyone with the link" in str(e.value)


def _ctx():
    class Ctx:
        sub, org_id = "sub-import", 1
    return Ctx()


@pytest.fixture
def cible(monkeypatch):
    monkeypatch.setattr(up, "_datastore_target", lambda sub, inp, fmt: {
        "kind": "datastore", "ns_id": 7, "namespace": "7", "format": fmt, "key": None})
    monkeypatch.setattr(upload_tokens, "check_target_access", lambda sub, t: None)


def test_resume_needs_the_hash_of_the_first_call(cible):
    with pytest.raises(AuthzDenied) as e:
        up._import(_ctx(), up.ImportInput(source={"kind": "url", "url": "https://x/a.csv"},
                                          datastore="7", resume_from=500))
    assert e.value.code == "missing_source_sha256"


def test_a_changed_file_is_refused_on_resume(cible, monkeypatch):
    monkeypatch.setattr(file_source, "resolve", lambda *a, **k: _rf())
    with pytest.raises(AuthzDenied) as e:
        up._import(_ctx(), up.ImportInput(source={"kind": "url", "url": "https://x/a.csv"},
                                          datastore="7", resume_from=500,
                                          source_sha256="0" * 64))
    assert e.value.code == "source_changed"


def test_an_unreadable_source_is_refused_by_name(cible, monkeypatch):
    def boom(*a, **k):
        raise file_source.FileSourceError("source url: more than 5 redirects.")
    monkeypatch.setattr(file_source, "resolve", boom)
    with pytest.raises(AuthzDenied) as e:
        up._import(_ctx(), up.ImportInput(source={"kind": "url", "url": "https://x/a.csv"},
                                          datastore="7"))
    assert e.value.code == "source_unreadable"


def test_the_fetch_follows_checked_redirects(cible, monkeypatch):
    vu = {}

    def resolve(source, **kw):
        vu.update(kw)
        raise file_source.FileSourceError("stop")
    monkeypatch.setattr(file_source, "resolve", resolve)
    with pytest.raises(AuthzDenied):
        up._import(_ctx(), up.ImportInput(source={"kind": "url", "url": "https://x/a.csv"},
                                          datastore="7"))
    assert vu["follow_redirects"] is True


def test_a_project_file_keeps_the_source_name(monkeypatch):
    monkeypatch.setattr(upload_tokens, "check_target_access", lambda sub, t: None)
    monkeypatch.setattr(file_source, "resolve", lambda *a, **k: _rf(name="deck.pdf",
                                                                     mime="application/pdf"))
    ecrit = {}

    def materialize(sub, target, data, ct):
        ecrit.update(target)
        return {"ok": True, "kind": "project_file", "filename": target["filename"],
                "bytes": len(data)}
    monkeypatch.setattr(upload_tokens, "materialize", materialize)
    out = up._import(_ctx(), up.ImportInput(
        source={"kind": "url", "url": "https://x/deck.pdf?sig=1"},
        target="project_file", project_id=3))
    assert ecrit["filename"] == "deck.pdf"
    assert out["source"]["url"] == "https://x/deck.pdf"
    assert len(out["source"]["sha256"]) == 64
