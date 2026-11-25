#!/usr/bin/env python3
"""Le Python d'un venv au regard du `requires-python` du tag (#967) : plancher, et règle.

Exécuté par le Python du SYSTÈME de la cible (`python3`), comme `declaration.py` :
bibliothèque standard seulement — `packaging` n'y est pas forcément. L'évaluation suit
PEP 440 pour des versions finales (`3`, `3.12`, `3.12.4`) : opérateurs `>=`, `>`, `<=`,
`<`, `==` (avec ou sans `.*`), `!=` (idem), `~=`, `===`, clauses séparées par des
virgules. Une spécification qu'on ne sait pas lire est REFUSÉE, jamais devinée.

    plancher_python.py plancher  <pyproject.toml>
        imprime le plancher MAJEUR.MINEUR (la clause `>=` ou `~=`), qui doit lui-même
        satisfaire la spécification.
    plancher_python.py satisfait <pyproject.toml> <version>
        code 0 si la version (`3.12.4`, ou `3.12`, lue `3.12.0`) satisfait la
        spécification, 1 sinon.
    plancher_python.py admet <pyproject.toml> <choix>
        le choix d'interpréteur d'une instance (`python` de sa déclaration) : code 0 si la
        spécification l'admet, 1 sinon. Un choix au patch (`3.12.8`) est jugé sur cette
        version exacte ; un choix MAJEUR.MINEUR (`3.12`) est admis s'il EXISTE une 3.12.x
        qui satisfait (`3.12` sous `>=3.12.4` : admis, 3.12.4 et au-delà conviennent).

Code 2, message sur stderr : pyproject sans `requires-python`, ou spécification illisible.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRES = re.compile(r'(?m)^requires-python\s*=\s*"([^"]*)"\s*$')
CLAUSE = re.compile(r"^(===|~=|==|!=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*)(\.\*)?$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
CHOIX = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")


class Illisible(Exception):
    pass


def specification(pyproject: Path) -> str:
    m = REQUIRES.search(pyproject.read_text(encoding="utf-8"))
    if not m:
        raise Illisible(f"{pyproject} : requires-python introuvable")
    return m.group(1)


def _clauses(spec: str) -> list[tuple[str, str, bool]]:
    clauses = []
    for brute in spec.split(","):
        m = CLAUSE.match(brute.strip())
        if not m:
            raise Illisible(f"requires-python « {spec} » : clause « {brute.strip()} » illisible")
        op, v, joker = m.group(1), m.group(2), bool(m.group(3))
        if joker and op not in ("==", "!="):
            raise Illisible(f"requires-python « {spec} » : « .* » seulement après == ou !=")
        if op == "~=" and "." not in v:
            raise Illisible(f"requires-python « {spec} » : ~= exige au moins deux composantes")
        clauses.append((op, v, joker))
    return clauses


def _t(v: str, n: int) -> tuple[int, ...]:
    parts = [int(x) for x in v.split(".")]
    return tuple(parts + [0] * (n - len(parts)))


def _clause(op: str, v: str, joker: bool, version: str) -> bool:
    if op == "===":
        return version == v
    if joker or op == "~=":
        prefixe = v.split(".") if joker else v.split(".")[:-1]
        candidat = list(version.split(".")) + ["0"] * len(prefixe)
        dedans = [int(x) for x in candidat[:len(prefixe)]] == [int(x) for x in prefixe]
        if op == "~=":
            return dedans and _clause(">=", v, False, version)
        return dedans if op == "==" else not dedans
    n = max(len(v.split(".")), len(version.split(".")))
    a, b = _t(version, n), _t(v, n)
    return {"==": a == b, "!=": a != b, "<=": a <= b, ">=": a >= b, "<": a < b, ">": a > b}[op]


def satisfait(spec: str, version: str) -> bool:
    if not VERSION.match(version):
        raise Illisible(f"version « {version} » illisible (MAJEUR.MINEUR[.MICRO] attendu)")
    return all(_clause(op, v, j, version) for op, v, j in _clauses(spec))


def admet(spec: str, choix: str) -> bool:
    """Le choix d'une instance : au patch, la version exacte ; sinon, une MAJEUR.MINEUR.x
    qui satisfait existe-t-elle. Les clauses ne changent de verdict qu'aux patchs qu'elles
    nomment : essayer 0 … (plus haut patch nommé + 1) couvre tous les cas."""
    if not CHOIX.match(choix):
        raise Illisible(f"choix « {choix} » illisible (MAJEUR.MINEUR ou MAJEUR.MINEUR.PATCH attendu)")
    if choix.count(".") == 2:
        return satisfait(spec, choix)
    patchs = [int(v.split(".")[2]) for _, v, _ in _clauses(spec) if v.count(".") >= 2]
    return any(satisfait(spec, f"{choix}.{z}") for z in range(max(patchs, default=0) + 2))


def plancher(spec: str) -> str:
    bas = [v for op, v, _ in _clauses(spec) if op in (">=", "~=")]
    if len(bas) != 1:
        raise Illisible(f"requires-python « {spec} » : une clause >= ou ~= attendue, "
                        f"et une seule (le plancher)")
    p = ".".join(bas[0].split(".")[:2])
    if "." not in p:
        raise Illisible(f"requires-python « {spec} » : plancher MAJEUR.MINEUR attendu")
    if not satisfait(spec, bas[0]):
        raise Illisible(f"requires-python « {spec} » : le plancher {bas[0]} ne la satisfait pas")
    return p


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "plancher":
            print(plancher(specification(Path(argv[1]))))
            return 0
        if len(argv) == 3 and argv[0] == "satisfait":
            return 0 if satisfait(specification(Path(argv[1])), argv[2]) else 1
        if len(argv) == 3 and argv[0] == "admet":
            return 0 if admet(specification(Path(argv[1])), argv[2]) else 1
    except (Illisible, OSError) as erreur:
        print(f"plancher Python : {erreur}", file=sys.stderr)
        return 2
    print("usage : plancher_python.py plancher <pyproject> | satisfait <pyproject> <version>"
          " | admet <pyproject> <choix>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
