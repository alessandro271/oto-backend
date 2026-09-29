"""Service unique `file_content.render_for_agent` — décision inline vs URL signée
partagée par gmail/drive/slack (DRY, ex-duplication)."""
import pytest

from oto_mcp import file_content, media_store


def test_render_inline_small_text():
    out = file_content.render_for_agent(b"# hello", "a.md", "text/markdown", sub="s", prefix="slack-files")
    assert out == {"filename": "a.md", "mimeType": "text/markdown", "size": 7,
                   "encoding": "text", "content": "# hello"}


def test_render_binary_uploads_and_signs(monkeypatch):
    calls = {}

    def fake_upload(prefix, sub, data, mime, filename):
        calls.update(prefix=prefix, sub=sub, filename=filename, size=len(data))
        return "https://signed/x"

    monkeypatch.setattr(media_store, "upload_private", fake_upload)
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    out = file_content.render_for_agent(b"\x89PNG\x00", "img.png", "image/png", sub="u1", prefix="slack-files")
    assert out["encoding"] == "url"
    assert out["url"] == "https://signed/x"
    assert out["expires_in"] == 3600
    assert calls == {"prefix": "slack-files", "sub": "u1", "filename": "img.png", "size": 5}


def test_render_large_text_goes_to_url(monkeypatch):
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/y")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    big = b"x" * (file_content.INLINE_TEXT_CAP + 1)
    out = file_content.render_for_agent(big, "big.md", "text/markdown", sub="s", prefix="p")
    assert out["encoding"] == "url"        # texte mais trop gros → URL, pas inline


def test_render_pdf_returns_its_text_inline_and_joins_the_original(monkeypatch):
    # Un agent dont le bac à sable ne joint pas notre stockage lisait un PDF servi
    # en URL seule : rien. Le texte vient maintenant inline, l'original à côté.
    from _pdf_texte import pdf_texte
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/pdf")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    out = file_content.render_for_agent(pdf_texte(["Facture 42", "Total 1 200,00"]),
                                        "f.pdf", "application/pdf", sub="s", prefix="p")
    assert out["encoding"] == "text" and out["format"] == "pdf-text"
    assert "Facture 42" in out["content"] and out["pages"] == 1
    assert out["truncated"] is False
    assert out["raw_url"] == "https://signed/pdf" and out["raw_expires_in"] == 3600


def test_render_pdf_without_text_falls_back_to_url_and_says_why(monkeypatch):
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/scan")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    from _pdf_texte import pdf_sans_texte
    out = file_content.render_for_agent(pdf_sans_texte(), "scan.pdf",
                                        "application/pdf", sub="s", prefix="p")
    assert out["encoding"] == "url" and out["url"] == "https://signed/scan"
    assert out["text_unavailable"].startswith("empty")


def test_render_unreadable_pdf_never_leaks_its_bytes_as_text(monkeypatch):
    # Des octets de PDF illisible restent décodables en UTF-8 : sans garde, ils
    # tombaient dans la règle du « petit texte » et sortaient inline.
    monkeypatch.setattr(media_store, "upload_private", lambda *a, **k: "https://signed/bad")
    monkeypatch.setattr(media_store, "presign_expiry", lambda: 3600)
    out = file_content.render_for_agent(b"%PDF-1.4\n" + b"x" * 50, "casse.pdf",
                                        "application/pdf", sub="s", prefix="p")
    assert out["encoding"] == "url" and "content" not in out
    assert out["text_unavailable"].startswith("failed")


def test_render_pdf_text_survives_a_storage_outage(monkeypatch):
    from _pdf_texte import pdf_texte

    def boom(*a, **k):
        raise media_store.MediaError(500, "no_s3", "down")

    monkeypatch.setattr(media_store, "upload_private", boom)
    out = file_content.render_for_agent(pdf_texte(["Contrat de prestation"]), "c.pdf",
                                        "application/pdf", sub="s", prefix="p")
    assert "Contrat de prestation" in out["content"]
    assert "raw_url" not in out and "indisponible" in out["raw_unavailable"]


def test_render_media_unavailable_raises(monkeypatch):
    def boom(*a, **k):
        raise media_store.MediaError(500, "no_s3", "down")

    monkeypatch.setattr(media_store, "upload_private", boom)
    with pytest.raises(file_content.MediaUnavailable):
        file_content.render_for_agent(b"\x00\x01", "x.bin", "application/octet-stream", sub="s", prefix="p")
