"""Le workflow d'une instance cible (`.github/workflows/deploy-cible.yml`, #967) : une
montée de version DÉCIDÉE, qui ne consulte que la cible et le tronc.

Ce que ce test fige, parce que chaque point est une décision (Alexis, 29/09/2026) :
- déclenché à la main seulement, avec un tag choisi ; rien d'autre ne l'appelle ;
- il ne consulte AUCUN autre déploiement : ni un autre workflow, ni ses runs, ni une
  autre instance — ni dans le workflow, ni dans les scripts qu'il exécute ;
- l'environnement de la cible doit exiger un relecteur, vérifié avant l'approbation ;
- un seul job derrière l'approbation : préprod d'abord, prod ensuite, qui exige que la
  préprod de la cible serve le tag ; le tag doit être sur la branche principale ;
- runner hébergé ; une entrée n'est jamais interpolée dans un script ;
- les valeurs d'une cible viennent de son environnement GitHub, jamais du dépôt.
"""
from __future__ import annotations

import ast
import pathlib
import re

import yaml

_RACINE = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _RACINE / ".github" / "workflows"
_TEXTE = (_WORKFLOWS / "deploy-cible.yml").read_text(encoding="utf-8")
_WF = yaml.safe_load(_TEXTE)
_JOBS = _WF["jobs"]
_DECLENCHEURS = _WF.get("on", _WF.get(True))     # PyYAML lit `on` comme True
_ETAPES = _JOBS["monter"]["steps"]
_RUNS = [s.get("run", "") for s in _ETAPES]

# Ce qui désignerait un AUTRE déploiement que celui de la cible : nos workflows et leurs
# noms, l'API des runs, nos gardes de mise en production, nos hôtes et notre machine.
_AUTRE_CHAINE = re.compile(
    r"deploy\.yml|deploy-canari|release\.yml|Deploy (prod|preprod)|actions/runs|"
    r"garde_preprod|workflow_run|workflow_call|oto-platform|/opt/deploy|oto-backend\.sh|"
    r"oto\.cx|oto\.ninja|oto-mcp-canari|active-(prod|canari)")


def _scripts_executes() -> list[pathlib.Path]:
    chemins = set(re.findall(r"(?:deploy/cible|scripts)/[\w.-]+\.(?:sh|py)", _TEXTE))
    # la porte et le déploiement, que `appeler.sh` fait exécuter sur la machine
    chemins |= {"deploy/cible/porte.sh", "deploy/cible/deployer.sh"}
    return sorted(_RACINE / c for c in chemins)


def _code(script: pathlib.Path) -> str:
    """Ce que le script EXÉCUTE : sans commentaires ni docstrings (un outil partagé peut
    raconter d'où il vient ; il ne doit pas y aller)."""
    texte = script.read_text(encoding="utf-8")
    if script.suffix == ".py":
        arbre = ast.parse(texte)
        for noeud in ast.walk(arbre):
            corps = getattr(noeud, "body", None)
            if (isinstance(corps, list) and corps and isinstance(corps[0], ast.Expr)
                    and isinstance(getattr(corps[0], "value", None), ast.Constant)
                    and isinstance(corps[0].value.value, str)):
                corps.pop(0)
        return ast.unparse(arbre)
    return "\n".join(l for l in texte.splitlines() if not l.lstrip().startswith("#"))


def test_declenche_a_la_main_seulement():
    assert set(_DECLENCHEURS) == {"workflow_dispatch"}
    assert {"cible", "tag", "etape", "action"} <= set(_DECLENCHEURS["workflow_dispatch"]["inputs"])


def test_ne_consulte_aucun_autre_deploiement():
    assert not _AUTRE_CHAINE.search(_TEXTE), _AUTRE_CHAINE.search(_TEXTE)
    scripts = _scripts_executes()
    assert len(scripts) >= 6
    for script in scripts:
        code = _code(script)
        assert not _AUTRE_CHAINE.search(code), (script.name, _AUTRE_CHAINE.search(code))


def test_le_garde_fou_reconnait_une_reference_a_une_autre_chaine():
    """Le motif mord : un appel à notre garde de production serait vu."""
    assert _AUTRE_CHAINE.search('run: python3 scripts/garde_preprod_verte.py "$TAG"')
    assert _AUTRE_CHAINE.search("gh api repos/x/y/actions/runs?branch=main")


def test_rien_d_autre_ne_l_appelle():
    for f in _WORKFLOWS.glob("*.yml"):
        if f.name != "deploy-cible.yml":
            texte = f.read_text(encoding="utf-8")
            assert "deploy-cible" not in texte and "deploy/cible" not in texte, f.name


def test_aucun_runner_de_nos_machines():
    for nom, job in _JOBS.items():
        assert job["runs-on"] == "ubuntu-latest", nom


def test_une_montee_a_la_fois_par_cible():
    assert _WF["concurrency"]["group"] == "deploy-cible-${{ inputs.cible }}"
    assert _WF["concurrency"]["cancel-in-progress"] is False


def test_la_protection_est_verifiee_avant_l_approbation():
    assert "environment" not in _JOBS["entrees"]
    assert _JOBS["entrees"]["permissions"] == {"actions": "read"}
    assert any('protection.sh "$DEPOT" "$CIBLE"' in s.get("run", "")
               for s in _JOBS["entrees"]["steps"])
    assert _JOBS["monter"]["needs"] == ["entrees"]


def test_un_seul_job_derriere_l_approbation():
    avec_env = [n for n, j in _JOBS.items() if "environment" in j]
    assert avec_env == ["monter"]
    assert _JOBS["monter"]["environment"] == "${{ inputs.cible }}"


def test_l_ordre_de_la_montee():
    def rang(fragment):
        return next(i for i, r in enumerate(_RUNS) if fragment in r)
    tronc = rang("git merge-base --is-ancestor")
    declaration = rang("declaration.py verifier")
    contrat = rang("scripts/contrat-front.py")
    preprod = rang('appeler.sh "$ACTION" preprod "$TAG"')
    constat_preprod = rang('constater.sh "$RUNNER_TEMP/declaration.json" preprod "$TAG"')
    prod = rang('appeler.sh "$ACTION" prod "$TAG"')
    assert tronc < declaration < contrat < preprod < constat_preprod < prod
    # le constat de la préprod garde la prod même quand on ne monte que la prod
    assert _ETAPES[constat_preprod]["if"] == "inputs.action == 'deployer'"


def test_une_entree_n_est_jamais_interpolee_dans_un_script():
    for nom, job in _JOBS.items():
        for etape in job.get("steps", []):
            assert "${{" not in etape.get("run", ""), (nom, etape.get("name"))


def test_les_valeurs_de_la_cible_viennent_de_son_environnement():
    for v in set(re.findall(r"\$\{\{\s*(vars|secrets)\.([A-Z_]+)", _TEXTE)):
        assert v[1].startswith("CIBLE_"), v
