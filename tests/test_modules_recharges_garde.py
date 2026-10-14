"""Aucun test ne retire une entrée de `sys.modules` sans passer par `_modules_recharges`.

Retirer un module de `sys.modules` puis le réimporter laisse le paquet parent pointer
sur le module rechargé si on ne rend que `sys.modules` : un patch posé ensuite par le
paquet vise un autre module que celui du code servi (cf. `tests/_modules_recharges.py`,
vécu sur oto-backend #1240). La fuite ne se voit que quand deux fichiers tombent sur le
même worker dans le bon ordre : seule une garde sur la forme l'empêche.

Formes refusées, hors de `tests/_modules_recharges.py` : `del sys.modules[...]`,
`sys.modules.pop(...)`, `<x>.delitem(sys.modules, ...)`. Un garde-fou d'inventaire se
prouve en lui présentant l'anomalie qu'il vise : d'où les cas synthétiques.
"""
from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

TESTS = pathlib.Path(__file__).resolve().parent
AIDE = TESTS / "_modules_recharges.py"


def _est_sys_modules(noeud: ast.AST) -> bool:
    return (isinstance(noeud, ast.Attribute) and noeud.attr == "modules"
            and isinstance(noeud.value, ast.Name) and noeud.value.id == "sys")


def retraits(source: str) -> list[int]:
    """Les lignes où `source` retire une entrée de `sys.modules`."""
    lignes = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Delete):
            lignes += [n.lineno for c in n.targets
                       if isinstance(c, ast.Subscript) and _est_sys_modules(c.value)]
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "pop" and _est_sys_modules(n.func.value):
                lignes.append(n.lineno)
            elif n.func.attr == "delitem" and n.args and _est_sys_modules(n.args[0]):
                lignes.append(n.lineno)
    return lignes


def test_aucun_test_ne_retire_un_module_a_la_main():
    fautes = [f"{f.relative_to(TESTS.parent)}:{ligne}"
              for f in sorted(TESTS.rglob("*.py")) if f != AIDE
              for ligne in retraits(f.read_text(encoding="utf-8"))]
    assert not fautes, (
        "retrait direct d'une entrée de `sys.modules` : le paquet parent garde le module "
        "rechargé et un patch posé ensuite par le paquet ne mord plus. Passe par "
        "`recharger_module` / `oublier_module` (tests/_modules_recharges.py) :\n  "
        + "\n  ".join(fautes))


@pytest.mark.parametrize("corps", [
    "del sys.modules['oto_mcp.tools.x']",
    "sys.modules.pop('oto_mcp.tools.x', None)",
    "monkeypatch.delitem(sys.modules, 'oto_mcp.tools.x', raising=False)",
    "for k in list(sys.modules):\n    del sys.modules[k]",
])
def test_elle_mord_sur_chaque_forme(corps):
    assert retraits(textwrap.dedent(f"import sys\n{corps}\n")), f"non vu : {corps}"


@pytest.mark.parametrize("corps", [
    "recharger_module(monkeypatch, 'oto_mcp.tools.x')",
    "monkeypatch.setitem(sys.modules, 'x', object())",
    "sys.modules.get('x')",
    "monkeypatch.delitem(os.environ, 'X')",
    "d.pop('x')",
])
def test_elle_laisse_passer_le_reste(corps):
    assert not retraits(f"import sys\n{corps}\n"), f"faux positif : {corps}"
