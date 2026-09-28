"""Bumper la version d'un document légal DÉCLARÉ, le temps d'un banc (#968).

Les métadonnées vivent dans `OTO_LEGAL_DOCS`, relue à chaque appel de
`legal_docs.current_docs()` : un bump se joue donc en reposant la variable, jamais en
mutant un dict partagé."""
from __future__ import annotations

import json

from oto_mcp import legal_docs


def bumper(monkeypatch, slug: str, version: str) -> None:
    docs = legal_docs.current_docs()
    docs[slug]["version"] = version
    monkeypatch.setenv("OTO_LEGAL_DOCS", json.dumps(docs))
