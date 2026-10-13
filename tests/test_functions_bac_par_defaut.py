"""Où l'exécuteur trouve le bac à sable : la variable d'abord, sinon `<arbre>/.bac-a-sable`
que le déploiement bleu/vert pose dans l'arbre de chaque couleur (`executor.DEFAUT`)."""
from __future__ import annotations

import pytest

from oto_mcp.functions import executor


def _poser(racine):
    (racine / "deno").parent.mkdir(parents=True, exist_ok=True)
    (racine / "deno").write_text("")
    (racine / "wheels").mkdir()
    pyodide = racine / executor._PYODIDE_REL
    pyodide.mkdir(parents=True)
    (pyodide / "pyodide.mjs").write_text("")
    return racine


def test_sans_variable_le_repertoire_de_l_arbre_sert(tmp_path, monkeypatch):
    monkeypatch.delenv(executor.ENV_DIR, raising=False)
    monkeypatch.setattr(executor, "DEFAUT", _poser(tmp_path / ".bac-a-sable"))
    assert executor.repertoire() == tmp_path / ".bac-a-sable"


def test_la_variable_l_emporte_sur_l_arbre(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "DEFAUT", _poser(tmp_path / ".bac-a-sable"))
    monkeypatch.setenv(executor.ENV_DIR, str(_poser(tmp_path / "ailleurs")))
    assert executor.repertoire() == tmp_path / "ailleurs"


def test_ni_variable_ni_arbre_rien_ne_s_execute(tmp_path, monkeypatch):
    monkeypatch.delenv(executor.ENV_DIR, raising=False)
    monkeypatch.setattr(executor, "DEFAUT", tmp_path / "absent")
    with pytest.raises(executor.BacASableAbsent):
        executor.repertoire()


def test_un_repertoire_incomplet_est_refuse(tmp_path, monkeypatch):
    monkeypatch.delenv(executor.ENV_DIR, raising=False)
    (tmp_path / ".bac-a-sable").mkdir()
    monkeypatch.setattr(executor, "DEFAUT", tmp_path / ".bac-a-sable")
    with pytest.raises(executor.BacASableAbsent):
        executor.repertoire()


def test_le_defaut_est_a_la_racine_du_depot_et_ignore_par_git():
    from pathlib import Path
    racine = Path(executor.__file__).resolve().parents[2]
    assert executor.DEFAUT == racine / ".bac-a-sable"
    assert "/.bac-a-sable/" in (racine / ".gitignore").read_text().splitlines()
