"""Le gabarit de déclaration d'une cible (`deploy/cible/declaration.gabarit.json`, #967)
porte la forme complète et AUCUNE valeur, reste en phase avec l'inventaire, et la doc
nomme tout ce que le workflow lit dans l'environnement d'une cible."""
from __future__ import annotations

import copy
import importlib.util
import json
import re

import pytest

from _banc_cible import DECLARATION, DEPOT

_spec = importlib.util.spec_from_file_location(
    "declaration_cible", DEPOT / "deploy" / "cible" / "declaration.py")
decl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decl)

GABARIT = json.loads((DEPOT / "deploy/cible/declaration.gabarit.json").read_text())
EXEMPLE = json.loads(DECLARATION.read_text())


def _forme(d, sans_env=False):
    if isinstance(d, dict):
        return {k: (None if sans_env and k == "env" else _forme(v, sans_env))
                for k, v in d.items()}
    return None


def test_le_gabarit_a_la_forme_d_une_declaration_conforme():
    assert _forme(GABARIT, sans_env=True) == _forme(EXEMPLE, sans_env=True)


def test_le_gabarit_porte_chaque_variable_exigee_et_rien_d_autre():
    for role, r in GABARIT["roles"].items():
        assert set(r["env"]) == set(decl.exigees_dans_env()) | {"OTO_ENV"}, role
        assert r["env"]["OTO_ENV"] == role


def test_le_gabarit_ne_porte_aucune_valeur():
    def feuilles(d, chemin=""):
        if isinstance(d, dict):
            for k, v in d.items():
                yield from feuilles(v, f"{chemin}.{k}")
        else:
            yield chemin, d
    for chemin, v in feuilles(GABARIT):
        if chemin.endswith(".OTO_ENV"):
            continue
        assert v in ("", None, []), chemin


def test_le_gabarit_tel_quel_est_refuse():
    with pytest.raises(decl.Refus):
        decl.valider(copy.deepcopy(GABARIT))


def test_oto_env_est_le_role_deploye():
    doc = copy.deepcopy(EXEMPLE)
    doc["roles"]["preprod"]["env"]["OTO_ENV"] = "prod"
    with pytest.raises(decl.Refus, match="OTO_ENV : doit valoir « preprod »"):
        decl.valider(doc)


def test_la_doc_nomme_tout_ce_que_le_workflow_lit():
    workflow = (DEPOT / ".github/workflows/deploy-cible.yml").read_text()
    doc = (DEPOT / "docs/instance-cible.md").read_text()
    lus = set(re.findall(r"\$\{\{\s*(?:vars|secrets)\.([A-Z_]+)", workflow))
    assert lus and all(f"`{n}`" in doc for n in lus), lus
