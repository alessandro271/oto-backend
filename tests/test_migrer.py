"""`oto-mcp migrer` (oto-backend#1105) : les arguments vont à Alembic tels quels, sur la
configuration du dépôt. Doublure de `CommandLine.run_cmd` : aucune base n'est ouverte."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import CommandLine

from oto_mcp.db._version_alembic import _REGISTRE
from oto_mcp.migrer import ALEMBIC_INI

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def appels(monkeypatch):
    """Doublure d'Alembic au point où il exécute : la commande choisie et ses arguments
    sont relevés, rien n'est joué (`CommandLine.run_cmd` appelle `fn(config, *pos, **kw)`)."""
    releve = []

    def relever(_ligne, config, options):
        fn, positionnels, nommes = options.cmd
        releve.append((fn.__name__, config,
                       tuple(getattr(options, k) for k in positionnels),
                       {k: getattr(options, k, None) for k in nommes}))

    monkeypatch.setattr(CommandLine, "run_cmd", relever)
    return releve


def _oto_mcp(monkeypatch, *arguments):
    from oto_mcp.cli import main
    monkeypatch.setattr(sys, "argv", ["oto-mcp", *arguments])
    with pytest.raises(SystemExit) as fin:
        main()
    return fin.value.code


def test_upgrade_head_passe_a_alembic_avec_la_configuration_du_depot(
        monkeypatch, tmp_path, appels):
    monkeypatch.chdir(tmp_path)  # le répertoire courant n'y est pour rien
    assert _oto_mcp(monkeypatch, "migrer", "upgrade", "head") == 0
    [(nom, config, positionnels, nommes)] = appels
    assert (nom, positionnels) == ("upgrade", ("head",))
    assert nommes["sql"] is False
    assert Path(config.config_file_name) == RACINE / "alembic.ini" == ALEMBIC_INI
    assert Path(config.get_main_option("script_location")) == _REGISTRE


@pytest.mark.parametrize("arguments, attendu", [
    (["upgrade", "head", "--sql"], ("upgrade", ("head",), {"sql": True})),
    (["current"], ("current", (), {"verbose": False})),
    (["stamp", "0001_depart"], ("stamp", (["0001_depart"],), {"sql": False})),
])
def test_les_arguments_passent_tels_quels(monkeypatch, appels, arguments, attendu):
    assert _oto_mcp(monkeypatch, "migrer", *arguments) == 0
    [(nom, _config, positionnels, nommes)] = appels
    assert nom == attendu[0] and positionnels == attendu[1]
    assert attendu[2].items() <= nommes.items()


@pytest.mark.parametrize("arguments", [[], ["-c", "autre.ini", "current"]])
def test_sans_commande_ou_avec_une_autre_configuration_refuse(monkeypatch, appels, arguments):
    assert _oto_mcp(monkeypatch, "migrer", *arguments) == 2
    assert appels == []
