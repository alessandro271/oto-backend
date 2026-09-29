"""Un PDF minimal qui PORTE du texte, fabriqué ici — pypdf sait écrire des pages
blanches, pas du texte. Une ligne par entrée de `lignes`, police standard, sans
dépendance : de quoi éprouver l'extraction sans fichier du disque."""
from __future__ import annotations

import io


def pdf_sans_texte() -> bytes:
    """Un vrai PDF qui ne porte aucun texte — ce que rend un scan sans OCR."""
    import pypdf
    w = pypdf.PdfWriter()
    w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def pdf_texte(lignes: list[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    flux = "BT /F1 11 Tf 14 TL 50 800 Td " + " ".join(
        f"({esc(l)}) Tj T*" for l in lignes) + " ET"
    corps = flux.encode("latin-1")
    objets = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(corps) + corps + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    positions = []
    for i, o in enumerate(objets, start=1):
        positions.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objets) + 1)
    for p in positions:
        out += b"%010d 00000 n \n" % p
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objets) + 1, xref)
    return bytes(out)
