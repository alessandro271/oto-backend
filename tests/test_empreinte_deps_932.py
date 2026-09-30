"""#932 — `/api/version` dit QUELLES dépendances sont installées, et si elles égalent le
verrou.

Chaque banc correspond à une façon réelle de se tromper :

- une couleur garde la version résolue le jour de sa création (mcp 1.27.2 en
  production, 1.29.1 sur la couleur en attente, mesuré le 30/09/2026) → écart de
  VERSION ;
- le venv porte des paquets que le manifeste ne demande pas (une CLI installée à la
  main, une bibliothèque décommissionnée) → paquet HORS VERROU ;
- oto-core a longtemps gardé le même `Version` pour des tags différents → même version,
  COMMIT différent ;
- le verrou est universel (toutes plateformes, tous extras) : un paquet Windows ou de
  l'extra `dev` n'est pas « manquant » sur la box.

Le verrou des bancs est écrit ici, à la main, dans la forme exacte d'un `uv.lock`
(révision 3) : le banc juge le PARCOURS, pas un fichier qui bougera à chaque montée.
Logique pure : aucun accès DB.
"""
from __future__ import annotations

import hashlib
import json
import sys

import pytest

from oto_mcp import empreinte_deps as e
from oto_mcp.empreinte_deps import tomllib

_COMMIT = "726f19ae89449739fff27822f66c4c6eb53efc50"

_VERROU = f'''
version = 1
revision = 3
requires-python = ">=3.10"

[[package]]
name = "projet"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "fastmcp", extra = ["apps"] }},
    {{ name = "le-coeur" }},
    {{ name = "tomli", marker = "python_full_version < '3.11'" }},
    {{ name = "pywin32", marker = "sys_platform == 'win32'" }},
]

[package.optional-dependencies]
dev = [
    {{ name = "pytest" }},
]

[[package]]
name = "fastmcp"
version = "3.4.7"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [
    {{ name = "mcp" }},
]

[package.optional-dependencies]
apps = [
    {{ name = "prefab-ui" }},
]

[[package]]
name = "mcp"
version = "1.27.2"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "prefab-ui"
version = "0.20.2"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "le-coeur"
version = "1.148.0"
source = {{ git = "https://example.invalid/le-coeur.git?rev=v1.148.0#{_COMMIT}" }}
dependencies = [
    {{ name = "jaraco-classes" }},
]

[[package]]
name = "jaraco-classes"
version = "3.4.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "tomli"
version = "2.4.1"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "pywin32"
version = "312"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "pytest"
version = "9.1.1"
source = {{ registry = "https://pypi.org/simple" }}
'''


def _verrou() -> dict:
    return tomllib.loads(_VERROU)


class _Dist:
    """Une distribution telle qu'`importlib.metadata` la rend : un nom tel qu'écrit
    dans METADATA, une version, et éventuellement un `direct_url.json`."""

    def __init__(self, nom, version, commit=None, direct_url=None):
        self.metadata = {"Name": nom}
        self.version = version
        if direct_url is None and commit:
            direct_url = json.dumps({"url": "https://example.invalid/x.git",
                                     "vcs_info": {"vcs": "git", "commit_id": commit}})
        self._direct_url = direct_url

    def read_text(self, nom):
        return self._direct_url if nom == "direct_url.json" else None


def _service(**remplace):
    """Ce que `uv sync --frozen` pose, sans extra, pour l'interpréteur des bancs."""
    base = {
        "projet": _Dist("projet", "0.1.0"),
        "fastmcp": _Dist("fastmcp", "3.4.7"),
        "mcp": _Dist("mcp", "1.27.2"),
        "prefab-ui": _Dist("prefab_ui", "0.20.2"),
        "le-coeur": _Dist("le-coeur", "1.148.0", commit=_COMMIT),
        "jaraco-classes": _Dist("jaraco.classes", "3.4.0"),
    }
    if sys.version_info < (3, 11):
        base["tomli"] = _Dist("tomli", "2.4.1")
    base.update(remplace)
    return [d for d in base.values() if d is not None]


def _juge(dists):
    return e.conformite(e.installees(dists), _verrou(), e.commits_vcs(dists))


# ── l'empreinte ──────────────────────────────────────────────────────────────

def test_l_empreinte_est_le_sha256_des_lignes_nom_version_triees():
    """La définition servie, recalculée à la main : un tiers doit pouvoir la refaire
    depuis un `pip freeze` sans lire notre code."""
    dists = [_Dist("Zeta_Pkg", "1.0"), _Dist("alpha", "2.0"), _Dist("jaraco.classes", "3.4.0")]
    attendu = hashlib.sha256(
        "alpha==2.0\njaraco-classes==3.4.0\nzeta-pkg==1.0".encode()).hexdigest()
    assert e.empreinte(e.installees(dists)) == attendu


def test_l_empreinte_change_avec_une_seule_version():
    """Le différentiel qui compte : deux couleurs qui ne diffèrent que par mcp."""
    une = e.empreinte(e.installees(_service()))
    autre = e.empreinte(e.installees(_service(mcp=_Dist("mcp", "1.29.1"))))
    assert une != autre


def test_l_empreinte_ne_depend_pas_de_l_ordre_ni_de_la_graphie_du_nom():
    a = [_Dist("Jaraco.Classes", "3.4.0"), _Dist("mcp", "1.27.2")]
    b = [_Dist("mcp", "1.27.2"), _Dist("jaraco_classes", "3.4.0")]
    assert e.empreinte(e.installees(a)) == e.empreinte(e.installees(b))


def test_deux_couleurs_amorcees_a_des_dates_differentes_ont_la_meme_empreinte():
    """L'exploitation compare `deps_sha` entre les deux couleurs après deux
    déploiements. `uv sync` ne touche jamais pip/setuptools/wheel : deux venv amorcés
    à des dates différentes les portent en versions différentes pour le même verrou.
    Absents du verrou, ils n'entrent pas dans l'empreinte."""
    amorce = e.amorcage_prescrit(_verrou())
    assert amorce == frozenset()
    bleue = _service() + [_Dist("pip", "26.1.2"), _Dist("setuptools", "59.6.0"),
                          _Dist("wheel", "0.47.0")]
    verte = _service() + [_Dist("pip", "23.0.1"), _Dist("setuptools", "79.0.1")]
    assert (e.empreinte(e.installees(bleue), amorce)
            == e.empreinte(e.installees(verte), amorce)
            == e.empreinte(e.installees(_service()), amorce))


def test_un_paquet_d_amorcage_PRESCRIT_par_le_verrou_compte_dans_l_empreinte():
    """La règle est celle de la conformité : prescrit, sa version compte."""
    verrou = _verrou()
    racine = next(p for p in verrou["package"] if p["name"] == "projet")
    racine["dependencies"].append({"name": "setuptools"})
    verrou["package"].append({"name": "setuptools", "version": "80.0.0",
                              "source": {"registry": "https://pypi.org/simple"}})
    amorce = e.amorcage_prescrit(verrou)
    assert amorce == frozenset({"setuptools"})
    une = e.empreinte(e.installees(_service(setuptools=_Dist("setuptools", "80.0.0"))), amorce)
    autre = e.empreinte(e.installees(_service(setuptools=_Dist("setuptools", "79.0.1"))), amorce)
    assert une != autre


def test_l_empreinte_servie_ignore_l_amorcage_sans_verrou(monkeypatch, tmp_path):
    dists = _service() + [_Dist("pip", "26.1.2")]
    monkeypatch.setattr(e.metadata, "distributions", lambda: dists)
    assert e.etat(tmp_path)["deps_sha"] == e.empreinte(e.installees(_service()))


# ── ce que le verrou prescrit ────────────────────────────────────────────────

def test_le_parcours_suit_les_extras_demandes_et_les_marqueurs_d_ici():
    prescrit = e.prescrits(_verrou())
    assert prescrit["prefab-ui"] == "0.20.2", "l'extra `apps` de fastmcp n'est pas suivi"
    assert prescrit["mcp"] == "1.27.2", "une transitive n'est pas suivie"
    assert "pywin32" not in prescrit, "un marqueur Windows a été pris pour vrai ici"
    assert ("tomli" in prescrit) == (sys.version_info < (3, 11))
    assert "pytest" not in prescrit, "l'extra `dev` du projet est prescrit au service"
    assert e.prescrits(_verrou(), ["dev"])["pytest"] == "9.1.1"


def test_un_verrou_sans_racine_est_une_erreur_pas_une_conformite():
    verrou = _verrou()
    verrou["package"] = [p for p in verrou["package"] if p["name"] != "projet"]
    with pytest.raises(e.VerrouIllisible, match="source « . »"):
        e.prescrits(verrou)


def test_une_reference_ambigue_est_une_erreur():
    verrou = _verrou()
    verrou["package"].append({"name": "mcp", "version": "1.29.1",
                              "source": {"registry": "https://pypi.org/simple"}})
    with pytest.raises(e.VerrouIllisible, match="`mcp`"):
        e.prescrits(verrou)


# ── la conformité ────────────────────────────────────────────────────────────

def test_le_jeu_du_service_est_conforme():
    assert _juge(_service()) == (True, [])


def test_le_jeu_de_la_ci_avec_dev_est_conforme():
    """`uv sync --frozen --extra dev` : le même verrou, un extra du projet en plus."""
    assert _juge(_service(pytest=_Dist("pytest", "9.1.1"))) == (True, [])


def test_une_version_restee_d_une_ancienne_resolution_est_un_ecart():
    conforme, liste = _juge(_service(mcp=_Dist("mcp", "1.29.1")))
    assert not conforme
    assert liste == ["mcp : 1.29.1 installé, 1.27.2 au verrou"]


def test_un_paquet_hors_manifeste_est_un_ecart():
    """Une CLI installée à la main dans le venv du service : `uv sync --frozen` exact
    la retire, et tant qu'elle est là l'arbre n'est pas celui du verrou."""
    dists = _service() + [_Dist("oto-cli", "1.7.1"), _Dist("typer", "0.26.7")]
    conforme, liste = _juge(dists)
    assert not conforme
    assert liste == ["oto-cli==1.7.1 : hors verrou", "typer==0.26.7 : hors verrou"]


def test_un_paquet_manquant_est_un_ecart():
    conforme, liste = _juge(_service(mcp=None))
    assert not conforme and liste == ["mcp : manquant (verrou 1.27.2)"]


def test_meme_version_autre_commit_est_un_ecart():
    """Le piège d'oto-core : même `Version`, tag différent."""
    autre = "0" * 40
    conforme, liste = _juge(_service(**{"le-coeur": _Dist("le-coeur", "1.148.0", commit=autre)}))
    assert not conforme
    assert liste == [f"le-coeur : commit {autre} installé, {_COMMIT} au verrou"]


def test_un_paquet_git_installe_hors_git_est_un_ecart():
    conforme, liste = _juge(_service(**{"le-coeur": _Dist("le-coeur", "1.148.0")}))
    assert not conforme and "aucun commit git" in liste[0]


def test_deux_dist_info_du_meme_paquet_sont_un_ecart():
    dists = _service() + [_Dist("mcp", "1.29.1")]
    conforme, liste = _juge(dists)
    assert not conforme and liste == ["mcp : 1.27.2,1.29.1 installé, 1.27.2 au verrou"]


def test_les_paquets_d_amorcage_du_venv_ne_sont_pas_un_ecart():
    """`python -m venv` pose pip et setuptools ; `uv sync --frozen` ne les retire
    jamais (mesuré avec uv 0.12.19). Les compter rendrait toute box non conforme."""
    dists = _service() + [_Dist("pip", "26.1.2"), _Dist("setuptools", "59.6.0"),
                          _Dist("wheel", "0.47.0")]
    assert _juge(dists) == (True, [])


# ── l'état servi ─────────────────────────────────────────────────────────────

def test_sans_verrou_l_arbre_n_est_pas_conforme_et_le_dit(tmp_path):
    etat = e.etat(tmp_path)
    assert etat["lock_sha"] is None
    assert etat["deps_conformes"] is False
    assert len(etat["deps_sha"]) == 64


def test_lock_sha_est_le_sha256_du_fichier_octet_pour_octet(tmp_path):
    (tmp_path / e.FICHIER_VERROU).write_text(_VERROU, encoding="utf-8")
    assert e.etat(tmp_path)["lock_sha"] == hashlib.sha256(_VERROU.encode()).hexdigest()


def test_un_verrou_illisible_leve(tmp_path):
    (tmp_path / e.FICHIER_VERROU).write_text("version = = 1", encoding="utf-8")
    with pytest.raises(e.VerrouIllisible):
        e.etat(tmp_path)


def test_calcule_une_fois_par_processus(monkeypatch, tmp_path):
    """Au démarrage, pas à chaque requête : le second appel ne relit rien."""
    appels = []
    monkeypatch.setattr(e, "etat", lambda racine: appels.append(racine) or {"x": 1})
    e.etat_au_demarrage.cache_clear()
    try:
        assert e.etat_au_demarrage(tmp_path) == e.etat_au_demarrage(tmp_path)
        assert appels == [tmp_path]
    finally:
        e.etat_au_demarrage.cache_clear()


# ── le verrou du dépôt ───────────────────────────────────────────────────────

def test_le_verrou_du_depot_se_parcourt_depuis_le_projet():
    """`uv.lock` est versionné (#932) : `/api/version` le parcourt au démarrage de
    chaque couleur. Un verrou que ce parcours refuserait ferait tomber le boot — on le
    sait ici, pas au déploiement. `uv lock --check` (CI) juge, lui, qu'il suit le
    manifeste."""
    from oto_mcp.version import racine_de_l_arbre
    verrou = tomllib.loads((racine_de_l_arbre() / e.FICHIER_VERROU).read_text("utf-8"))
    service = e.prescrits(verrou)
    assert {"oto-mcp", "fastmcp", "mcp", "oto-core", "prefab-ui"} <= set(service)
    assert "pytest" not in service and "pytest" in e.prescrits(verrou, ["dev"])
    assert "oto-core" in e.commits_du_verrou(verrou), "oto-core n'est plus pris dans git"
