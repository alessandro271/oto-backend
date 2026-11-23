"""Le changement de plancher Python, avec le VRAI uv (`uv venv`, `uv python install`,
`uv sync --frozen` sous `UV_PYTHON_DOWNLOADS=never`) : ce que les doublures ne peuvent pas
dire, c'est QUEL interpréteur uv retient. Ici : le venv recréé l'est sur l'interpréteur
géré du rôle (`/opt/<i>/python`), jamais un Python du système, et `uv sync` le garde ; à
plancher égal, `uv sync` ne recrée rien.

Le passage 3.10 → 3.12 est rejoué en 3.11 → 3.12 sur deux interpréteurs gérés par uv déjà
présents sur la machine (aucun téléchargement : sans eux, le module est ignoré). Le tag est
un projet minimal sans dépendance, pour que `uv sync` reste hors réseau ; le reste de la
chaîne est celui de `_banc_cible.py` (root simulé, racine jetable).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from _banc_cible import Banc

UV = shutil.which("uv")  # un chemin, pas un travail
ANCIEN, NOUVEAU = "3.11", "3.12"


def _gere(version: str) -> bool:
    if not UV:
        return False
    fini = subprocess.run([UV, "python", "find", "--managed-python", "--no-config", version],
                          capture_output=True, text=True,
                          env={**os.environ, "UV_PYTHON_DOWNLOADS": "never"})
    return fini.returncode == 0


@pytest.fixture
def uv_reel():
    """Sondé au test, jamais à l'import (un import définit, il ne travaille pas)."""
    if not (_gere(ANCIEN) and _gere(NOUVEAU)):
        pytest.skip(f"uv et ses interpréteurs gérés {ANCIEN} et {NOUVEAU} requis (aucun téléchargement)")


def _plancher(banc: Banc, version: str) -> None:
    """Un tag minimal : son plancher, et le verrou qui va avec (hors réseau)."""
    (banc.tag / "pyproject.toml").write_text(
        '[project]\nname = "banc-plancher"\nversion = "0"\n'
        f'requires-python = ">={version}"\ndependencies = []\n\n[tool.uv]\npackage = false\n',
        encoding="utf-8")
    subprocess.run([UV, "lock", "--quiet", "--offline", "--no-config"], cwd=banc.tag, check=True,
                   env={**os.environ, "UV_PYTHON_DOWNLOADS": "never"})


@pytest.fixture
def banc(tmp_path, uv_reel):
    banc = Banc(tmp_path)
    # uv : le vrai, journalisé.
    (banc.bin / "uv").write_text(
        f'#!/bin/bash\necho "uv $*" >> "$BANC_TRACE"\nexec {UV} --no-config "$@"\n')
    # Les interpréteurs gérés du rôle sont ceux déjà posés par uv sur la machine : l'amorce
    # les y « installe » sans rien télécharger.
    pythons = subprocess.run([UV, "python", "dir"], capture_output=True, text=True,
                             check=True).stdout.strip()
    (banc.racine / "opt/exemple").mkdir(parents=True)
    (banc.racine / "opt/exemple/python").symlink_to(pythons)
    _plancher(banc, ANCIEN)
    return banc


def _monter(banc: Banc, tag: str) -> subprocess.CompletedProcess:
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "deployer", "prod", tag, UV_PYTHON_DOWNLOADS="never",
                       HOME=str(banc.racine))
    assert fini.returncode == 0, fini.stdout + fini.stderr
    return fini


def _venv(banc: Banc, couleur: str) -> pathlib.Path:
    return banc.racine / f"opt/exemple/prod-{couleur}/.venv"


def _version(banc: Banc, couleur: str) -> str:
    return subprocess.run([str(_venv(banc, couleur) / "bin/python"), "-c",
                           'import sys; print("%d.%d" % sys.version_info[:2])'],
                          capture_output=True, text=True, check=True).stdout.strip()


def _home(banc: Banc, couleur: str) -> pathlib.Path:
    for ligne in (_venv(banc, couleur) / "pyvenv.cfg").read_text().splitlines():
        cle, _, valeur = ligne.partition("=")
        if cle.strip() == "home":
            return pathlib.Path(valeur.strip()).resolve()
    raise AssertionError("pyvenv.cfg sans home")


def test_plancher_qui_monte_sur_le_vrai_uv(banc):
    pythons = (banc.racine / "opt/exemple/python").resolve()
    _monter(banc, "v1.0.0")                       # naissance : bleue sert, verte installée
    _monter(banc, "v1.1.0")                       # la bleue sert de nouveau
    for c in ("blue", "green"):
        assert _version(banc, c) == ANCIEN
        (_venv(banc, c) / "temoin").write_text(c)
    cfg_avant = {c: (_venv(banc, c) / "pyvenv.cfg").read_text() for c in ("blue", "green")}

    # plancher égal : `uv sync --frozen` (et son environnement) ne recrée aucun venv
    _monter(banc, "v1.2.0")
    for c in ("blue", "green"):
        assert (_venv(banc, c) / "temoin").exists()
        assert (_venv(banc, c) / "pyvenv.cfg").read_text() == cfg_avant[c]
    assert not [c for c in banc.commandes() if c.startswith("uv venv")]
    assert banc.lire("/etc/exemple/prod/active") == "green\n"

    # plancher relevé : la bleue (inactive) est recréée sur l'interpréteur géré du rôle
    _plancher(banc, NOUVEAU)
    fini = _monter(banc, "v2.0.0")
    assert f"venv {ANCIEN} → {NOUVEAU} recréé" in fini.stdout
    assert _version(banc, "blue") == NOUVEAU and not (_venv(banc, "blue") / "temoin").exists()
    assert _home(banc, "blue").is_relative_to(pythons)
    assert "uv sync --frozen --quiet" in banc.commandes()
    # la verte, qui servait, n'a pas bougé : c'est le retour arrière
    assert _version(banc, "green") == ANCIEN and (_venv(banc, "green") / "temoin").exists()
    assert (_venv(banc, "green") / "pyvenv.cfg").read_text() == cfg_avant["green"]

    # retour arrière : la verte repart sur son venv d'avant
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v2.0.0", UV_PYTHON_DOWNLOADS="never",
                       HOME=str(banc.racine))
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert banc.lire("/etc/exemple/prod/active") == "green\n"
    assert _version(banc, "green") == ANCIEN and _version(banc, "blue") == NOUVEAU

    # la montée suivante (bleue de nouveau) : déjà au plancher, rien n'est recréé
    (_venv(banc, "blue") / "temoin").write_text("blue")
    _monter(banc, "v2.1.0")
    assert (_venv(banc, "blue") / "temoin").exists()
    assert not [c for c in banc.commandes() if c.startswith("uv venv")]
    # et celle d'après migre enfin la verte
    _monter(banc, "v2.2.0")
    assert _version(banc, "green") == NOUVEAU and _home(banc, "green").is_relative_to(pythons)
