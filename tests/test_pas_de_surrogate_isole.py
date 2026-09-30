"""Garde : aucun littéral de `oto_mcp` ne produit un surrogate isolé (oto-backend#1106).

Un `\\ud800` écrit dans une chaîne NON brute devient, à la compilation, le caractère
U+D800 seul — que l'UTF-8 refuse d'encoder. Sous Python 3.13, le hook d'import de
beartype réencode la source : l'import du module échoue (`UnicodeEncodeError:
surrogates not allowed`), et tout banc qui monte le serveur tombe à la collecte. Vécu
avec une docstring qui CITAIT la séquence d'échappement : l'écrire `\\\\ud800` ou en
chaîne brute.

Parcours AST des constantes chaîne (docstrings comprises) de chaque module du paquet.
"""
from __future__ import annotations

import ast
from pathlib import Path

PAQUET = Path(__file__).resolve().parents[1] / "oto_mcp"


def _surrogates(source: str) -> list[tuple[int, str]]:
    return [(noeud.lineno, f"U+{ord(c):04X}")
            for noeud in ast.walk(ast.parse(source))
            if isinstance(noeud, ast.Constant) and isinstance(noeud.value, str)
            for c in noeud.value if 0xD800 <= ord(c) <= 0xDFFF]


def test_aucun_litteral_du_paquet_ne_produit_un_surrogate_isole():
    fautes = [f"{chemin.relative_to(PAQUET.parent)}:{ligne} {code}"
              for chemin in sorted(PAQUET.rglob("*.py"))
              for ligne, code in _surrogates(chemin.read_text(encoding="utf-8"))]
    assert not fautes, "surrogate isolé dans un littéral (écrire \\\\uXXXX) : " + ", ".join(fautes)


def test_la_garde_voit_un_surrogate_et_pas_son_echappement():
    assert _surrogates('"""un `\\ud800` cité"""') == [(1, "U+D800")]
    assert _surrogates('"""un `\\\\ud800` cité"""') == []
    assert _surrogates('r"""un `\\ud800` cité"""') == []
