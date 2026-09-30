"""Garde-fou carte↔client : un champ déclaré FACULTATIF sur la carte ne doit pas
être exigé par le client oto-core.

Deux repos, deux déclarations du même fait — « ce champ est-il nécessaire ? » :

- la **carte** (`providers/<nom>.py`) est le contrat avec l'UTILISATEUR : elle pilote le
  formulaire dashboard, la validation REST et le packing au coffre
  (`CredentialField.required`) ;
- le **client** (oto-core) est le contrat avec l'API : `self.x = require(x, "NOM")`
  (`oto.tools.common.credentials`) = obligatoire (il LÈVE `MissingCredential` si
  absent), `self.x = x` = facultatif. La lib ne lit aucun secret (v1.148.0) : le
  consommateur passe tout.

Rien ne relie structurellement les deux. Le sens dangereux est **carte plus laxiste
que le client** : la pose réussit (le champ est facultatif au formulaire), puis le
client lève à la PREMIÈRE utilisation — un connecteur qui se configure et ne marche
pas. Cette sonde le ferme statiquement.

Le sens inverse (carte plus stricte que le client) n'est PAS dérivable du code :
« l'API n'a pas besoin de ce champ » est une connaissance de l'API, pas du
programme. Il se couvre par un test explicite par connecteur — cf.
`test_zohodesk_card.py` (l'`org_id` requis rendait le connecteur imposable, 28/07).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from pathlib import Path

import pytest

from oto_mcp import providers

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"


def _client_class(tool_module: str):
    """Classe cliente oto-core d'un module d'outil, via la convention
    `def _client() -> <Classe>` + son import (même résolution que
    `test_tools_client_methods_exist.py`). None si hors convention."""
    path = _TOOLS_DIR / f"{tool_module}.py"
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(), filename=str(path))
    clsname = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_client":
            if isinstance(node.returns, ast.Name):
                clsname = node.returns.id
            break
    if not clsname:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(a.name == clsname for a in node.names):
                try:
                    return getattr(importlib.import_module(node.module), clsname)
                except Exception:  # noqa: BLE001 — extra non installé
                    return None
    return None


def _is_require(func: ast.expr) -> bool:
    """L'aide commune de la lib, `oto.tools.common.credentials.require`, importée nue
    (`require(…)`, la forme de tous les clients) ou par son module (`credentials.require`)."""
    return ((isinstance(func, ast.Name) and func.id == "require")
            or (isinstance(func, ast.Attribute) and func.attr == "require"))


def _hard_required_from_source(src: str) -> set[str]:
    """Paramètres EXIGÉS dans un source d'`__init__` : tout paramètre passé en premier
    argument à `require(<param>, "NOM")`, où que soit l'appel dans le corps
    (`self.x = require(x, …)`, `str(require(x, …))`, `build(…, credentials=require(x, …))`).
    Un paramètre simplement recopié (`self.x = x`) est facultatif.
    ⚠️ `textwrap.dedent`, PAS `inspect.cleandoc` (fait pour les docstrings : il
    mange l'indentation du corps → IndentationError)."""
    tree = ast.parse(textwrap.dedent(src))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    out: set[str] = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and _is_require(node.func) and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id in params):
            out.add(node.args[0].id)
    return out


def _hard_required_params(cls) -> set[str]:
    """Idem, pour une classe cliente réelle."""
    try:
        return _hard_required_from_source(inspect.getsource(cls.__init__))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover
        return set()


def _clash(cls, optional: set[str]) -> list[str]:
    """Champs FACULTATIFS sur la carte que le client exige."""
    return sorted(optional & _hard_required_params(cls))


def _cases():
    """(connecteur, module, champs facultatifs de la carte)."""
    out = []
    for c in providers.REGISTRY.values():
        optional = {f.name for f in c.secret_fields if not f.required}
        if not optional:
            continue
        for mod in (c.modules or (c.name,)):
            out.append((c.name, mod, optional))
    return out


_CASES = _cases()


def test_probe_actually_covers_something():
    """Anti-couverture-fantôme : une sonde qui ne teste plus rien passe en silence.
    `zohodesk` est le cas d'école (28/07) — s'il sort de la couverture, c'est que la
    convention `_client() -> Classe` a bougé et que la sonde s'est vidée."""
    assert _CASES, "aucun connecteur avec champ facultatif — sonde inerte ?"
    assert any(c == "zohodesk" for c, _, _ in _CASES), (
        "zohodesk doit rester couvert (cas d'école du garde-fou carte↔client)")


@pytest.mark.parametrize("connector, tool_module, optional", _CASES,
                         ids=[f"{c}:{m}" for c, m, _ in _CASES])
def test_optional_card_field_is_not_required_by_client(connector, tool_module, optional):
    cls = _client_class(tool_module)
    if cls is None:
        pytest.skip(f"{tool_module}: pas de `_client() -> Classe` résoluble")
    clash = _clash(cls, optional)
    assert not clash, (
        f"carte `{connector}` déclare {clash} FACULTATIF(S), mais {cls.__name__} "
        f"les exige (`require(...)`) → la pose réussira et le connecteur lèvera "
        f"`MissingCredential` au premier appel. Rendre le champ optionnel côté client "
        f"(`self.x = x`) ou requis sur la carte (`required=True`).")


def test_detector_recognises_the_lib_forms():
    """La sonde distingue bien requis (`require(x, "NOM")`, sous toutes ses formes
    d'appel) et facultatif (`self.x = x`) — sinon elle ne prouverait rien."""
    src = (
        "class C:\n"
        "    def __init__(self, a=None, b=None, c=None, d=None):\n"
        "        self.a = require(a, 'A')\n"
        "        self.b = b\n"
        "        self.c = str(require(c, 'C')).strip()\n"
        "        self.d = credentials.require(d, 'D')\n"
        "        tok = self._token()\n"
        "        self.t = require(tok, 'T')\n")
    init = src.split("class C:\n")[1]
    assert _hard_required_from_source(init) == {"a", "c", "d"}


def test_probe_bites_on_the_real_lib():
    """La sonde lit le motif RÉEL de la lib, pas seulement un source de banc : sur le
    client du cas d'école, elle voit les champs exigés, et la garde échouerait si la
    carte les déclarait facultatifs. Si la lib change encore de motif (comme en v1.148.0,
    `x or require_secret(…)` → `require(x, …)`), ce test rougit au lieu de laisser la
    garde passer à vide."""
    from oto.tools.zohodesk.client import ZohoDeskClient

    hard = _hard_required_params(ZohoDeskClient)
    assert {"client_id", "client_secret"} <= hard, hard
    assert "org_id" not in hard  # facultatif côté client ET carte (cas d'école du 28/07)
    assert _clash(ZohoDeskClient, {"client_id", "org_id"}) == ["client_id"]
    # Couverture : la sonde voit des champs exigés sur une part réelle des clients
    # couverts — une sonde qui ne trouve rien nulle part est inerte.
    classes = {cls for _, m, _ in _CASES if (cls := _client_class(m)) is not None}
    assert classes, "aucun client résolu — sonde inerte ?"
    assert any(_hard_required_params(cls) for cls in classes), (
        "aucun client couvert n'exige un champ : le motif `require(x, …)` n'est plus reconnu")
