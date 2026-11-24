"""Changer le plancher Python d'une instance cible sans coupure, retour arrière intact.

Le `requires-python` du tag fixe le Python des venvs. Les deux couleurs ne changent jamais
de Python ensemble : la couleur en service garde le sien tant qu'elle sert, et la couleur
inactive EST le retour arrière jusqu'à la montée suivante. D'où la règle :

- l'amorce (`deploy/cible/amorcer.sh`), qui tourne à chaque montée, à chaque retour et
  avant la maintenance, DIT un venv que le `requires-python` du tag n'admet pas et ne le
  touche pas ;
- la montée (`bg_avant_installation`, deploy/cible/deployer.sh) recrée le venv de la SEULE
  couleur qu'elle installe, avant le `git reset`, s'il ne satisfait pas le
  `requires-python` du tag ou diffère du choix de l'instance (`python` de la
  déclaration) ; elle refuse sans rien toucher si cette couleur tourne, et remet code et
  venv d'avant si l'installation échoue (`bg_apres_installation`).

Banc : `_banc_cible.py` (root simulé, racine jetable ; `uv venv --python X` y pose un venv
dont l'interpréteur répond X). Un témoin posé dans chaque venv dit s'il a été recréé.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from _banc_cible import DECLARATION, SHA, Banc

UNITE = "exemple-prod"


@pytest.fixture
def banc(tmp_path):
    return Banc(tmp_path)


def plancher(banc: Banc, version: str) -> None:
    """Le `requires-python` du tag que la porte a extrait."""
    py = banc.tag / "pyproject.toml"
    texte, n = re.subn(r'^requires-python\s*=\s*"[^"]*"', f'requires-python = ">={version}"',
                       py.read_text(encoding="utf-8"), count=1, flags=re.M)
    assert n == 1
    py.write_text(texte, encoding="utf-8")


def venv(banc: Banc, couleur: str) -> str:
    python = banc.racine / f"opt/exemple/prod-{couleur}/.venv/bin/python"
    return subprocess.run([str(python)], capture_output=True, text=True).stdout.strip()


def poser_temoins(banc: Banc) -> None:
    for c in ("blue", "green"):
        (banc.racine / f"opt/exemple/prod-{c}/.venv/temoin").write_text(c)


def temoin(banc: Banc, couleur: str) -> bool:
    return (banc.racine / f"opt/exemple/prod-{couleur}/.venv/temoin").exists()


def actif(banc: Banc) -> str:
    return banc.lire("/etc/exemple/prod/active").strip()


def venvs_crees(banc: Banc) -> list[str]:
    return [c for c in banc.commandes() if c.startswith("uv venv")]


def monter(banc: Banc, tag: str, **env: str) -> subprocess.CompletedProcess:
    banc.trace.write_text("")
    return banc.lancer("deployer.sh", "deployer", "prod", tag, **env)


def choisir(banc: Banc, python: str):
    """La déclaration de l'instance, avec son choix d'interpréteur (`python`)."""
    doc = json.loads(DECLARATION.read_text())
    doc["python"] = python
    chemin = banc.racine / "declaration_python.json"
    chemin.write_text(json.dumps(doc))
    return chemin


@pytest.fixture
def role_en_310(banc):
    """Un rôle né en 3.10, monté deux fois : la bleue sert, les deux venvs en 3.10."""
    assert monter(banc, "v1.0.0").returncode == 0
    assert monter(banc, "v1.1.0").returncode == 0
    assert actif(banc) == "blue" and venv(banc, "blue") == venv(banc, "green") == "3.10"
    poser_temoins(banc)
    return banc


def test_naissance_les_deux_venvs_au_plancher(banc):
    fini = monter(banc, "v1.0.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    for c in ("blue", "green"):
        assert (f"uv venv --quiet --seed --python 3.10 --python-preference only-managed "
                f"/opt/exemple/prod-{c}/.venv") in banc.commandes()
        assert venv(banc, c) == "3.10"
    assert len(venvs_crees(banc)) == 2          # l'amorce, et la montée n'y ajoute rien
    assert "recréé" not in fini.stdout


def test_meme_plancher_la_montee_ne_touche_aucun_venv(role_en_310):
    banc = role_en_310
    fini = monter(banc, "v1.2.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert venvs_crees(banc) == []
    assert temoin(banc, "blue") and temoin(banc, "green")
    assert "recréé" not in fini.stdout and "n'admet pas" not in fini.stdout
    assert actif(banc) == "green"


def test_nouveau_plancher_seule_la_couleur_installee_change_de_python(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    fini = monter(banc, "v2.0.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    sortie = fini.stdout.replace(str(banc.racine), "")
    # l'amorce DIT l'écart des deux venvs, sans refuser ni recréer…
    assert ("/opt/exemple/prod-blue/.venv est en Python 3.10, que le requires-python du tag "
            "n'admet pas (plancher 3.12)") in sortie
    # … la montée recrée la verte (inactive), une fois, avant de l'installer…
    assert venvs_crees(banc) == [
        "uv venv --quiet --seed --python 3.12 --python-preference only-managed "
        "/opt/exemple/prod-green/.venv"]
    assert "venv 3.10 → 3.12 recréé dans /opt/exemple/prod-green" in sortie
    cmds = banc.commandes()
    assert cmds.index(venvs_crees(banc)[0]) < cmds.index("git reset --hard v2.0.0") \
        < cmds.index("uv sync --frozen --quiet")
    assert venv(banc, "green") == "3.12" and not temoin(banc, "green")
    assert not (banc.racine / "opt/exemple/prod-green/.venv.avant-plancher").exists()
    # … et la bleue, qui servait pendant toute la montée, n'a pas bougé.
    assert venv(banc, "blue") == "3.10" and temoin(banc, "blue")
    assert actif(banc) == "green"


@pytest.mark.parametrize("plancher_du_retour", ["3.12", "3.10"])
def test_retour_juste_apres_la_couleur_d_avant_garde_son_python(role_en_310, plancher_du_retour):
    """Le retour lance l'amorce du tag NOMMÉ dans la commande (la porte extrait son
    `deploy/`) : le tag qui vient de monter (plancher 3.12) ou celui d'avant (3.10). Les
    deux passent, tant qu'ils portent la tolérance."""
    banc = role_en_310
    plancher(banc, "3.12")
    assert monter(banc, "v2.0.0").returncode == 0
    plancher(banc, plancher_du_retour)
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v2.0.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert "REFUS" not in fini.stdout + fini.stderr
    assert actif(banc) == "blue"
    assert venv(banc, "blue") == "3.10" and temoin(banc, "blue")
    assert venv(banc, "green") == "3.12"
    assert venvs_crees(banc) == []
    assert not [c for c in banc.commandes() if c.startswith("git reset")]


def test_une_amorce_d_avant_la_tolerance_refuse_le_retour(role_en_310):
    """La preuve de l'ORDRE : l'amorce qui refusait un venv hors du plancher (telle
    qu'avant ce changement) bloque le retour dès qu'une couleur a changé de Python. La
    tolérance doit donc être dans un tag monté AVANT celui qui relève le plancher, et
    aucun tag plus ancien ne doit plus être nommé sur ce rôle."""
    banc = role_en_310
    plancher(banc, "3.12")
    assert monter(banc, "v2.0.0").returncode == 0
    plancher(banc, "3.10")
    amorce = banc.tag / "deploy/cible/amorcer.sh"
    texte, n = re.subn(
        r'  if \[ "\$version" = illisible \] \|\| .*?\n  fi\n',
        '  [ "$version" = "$PYV" ] || refus "$arbre/.venv est en Python $version, '
        'le tag exige $PYV — recréer ce venv"\n',
        amorce.read_text(encoding="utf-8"), count=1, flags=re.S)
    assert n == 1
    amorce.write_text(texte, encoding="utf-8")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v1.1.0")
    assert fini.returncode == 1
    assert "prod-green/.venv est en Python 3.12, le tag exige 3.10" in fini.stderr
    assert actif(banc) == "green"


def test_la_maintenance_ne_recree_rien(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    assert monter(banc, "v2.0.0").returncode == 0
    banc.trace.write_text("")
    fini = banc.lancer("amorcer.sh", "prod", "maintenance")
    assert fini.returncode == 0, fini.stderr
    assert venvs_crees(banc) == []
    assert venv(banc, "blue") == "3.10" and temoin(banc, "blue")
    assert venv(banc, "green") == "3.12"


def test_la_montee_suivante_migre_l_autre_couleur_puis_plus_rien(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    assert monter(banc, "v2.0.0").returncode == 0
    fini = monter(banc, "v2.1.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert venvs_crees(banc) == [
        "uv venv --quiet --seed --python 3.12 --python-preference only-managed "
        "/opt/exemple/prod-blue/.venv"]
    assert venv(banc, "blue") == venv(banc, "green") == "3.12"
    assert actif(banc) == "blue"
    # les deux au plancher : la montée d'après est celle d'avant le changement
    fini = monter(banc, "v2.2.0")
    assert fini.returncode == 0 and venvs_crees(banc) == []
    assert "n'admet pas" not in fini.stdout


def test_couleur_cible_qui_tourne_encore_refus_sans_rien_toucher(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    fini = monter(banc, "v2.0.0", BANC_UNITE_ACTIVE=f"{UNITE}@green")
    assert fini.returncode == 1
    assert f"{UNITE}@green tourne encore, son venv (Python 3.10) n'est pas touché" in fini.stderr
    assert "REFUS avant installation" in fini.stdout
    assert venv(banc, "green") == "3.10" and temoin(banc, "green")
    assert venv(banc, "blue") == "3.10" and temoin(banc, "blue")
    assert venvs_crees(banc) == []
    assert not [c for c in banc.commandes() if c.startswith(("git reset", "uv sync"))]
    assert actif(banc) == "blue"


def test_venv_neuf_impossible_l_ancien_est_remis(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    fini = monter(banc, "v2.0.0", BANC_UV_VENV_KO="1")
    assert fini.returncode == 1
    assert "venv Python 3.12 non créé" in fini.stderr
    assert venv(banc, "green") == "3.10" and temoin(banc, "green")
    assert not (banc.racine / "opt/exemple/prod-green/.venv.avant-plancher").exists()
    assert not [c for c in banc.commandes() if c.startswith(("git reset", "uv sync"))]
    assert actif(banc) == "blue"


# --- la règle : satisfaire le `requires-python` du tag, pas l'égaler -----------------
def test_un_venv_au_dessus_du_plancher_reste_tel_quel(role_en_310):
    """Venv 3.12 sous un tag `>=3.10` : il satisfait le tag, la montée n'y touche pas."""
    banc = role_en_310
    plancher(banc, "3.12")
    assert monter(banc, "v2.0.0").returncode == 0        # la verte passe en 3.12
    plancher(banc, "3.10")                               # un tag au plancher 3.10
    (banc.racine / "opt/exemple/prod-green/.venv/temoin").write_text("green")
    assert monter(banc, "v2.1.0").returncode == 0        # bleue installée : 3.10, admise
    assert venvs_crees(banc) == []
    fini = monter(banc, "v2.2.0")                        # verte installée : 3.12, admise
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert venvs_crees(banc) == []
    assert venv(banc, "green") == "3.12" and temoin(banc, "green")
    assert "n'admet pas" not in fini.stdout and "recréé" not in fini.stdout


def test_un_requires_python_en_intervalle_s_evalue(role_en_310):
    """Un intervalle s'évalue comme tel : `>=3.11,<3.13` n'admet pas 3.10, et le venv
    recréé l'est au plancher de l'intervalle."""
    banc = role_en_310
    py = banc.tag / "pyproject.toml"
    py.write_text(re.sub(r'(?m)^requires-python\s*=\s*"[^"]*"',
                         'requires-python = ">=3.11,<3.13"', py.read_text(encoding="utf-8")))
    fini = monter(banc, "v2.0.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert venvs_crees(banc) == [
        "uv venv --quiet --seed --python 3.11 --python-preference only-managed "
        "/opt/exemple/prod-green/.venv"]


# --- l'ancien venv est gardé jusqu'à la fin de l'installation ----------------------
def test_fetch_en_echec_la_couleur_garde_son_venv(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    fini = monter(banc, "v2.0.0", BANC_GIT_FETCH_KO="1", BANC_GIT_TETES="1")
    assert fini.returncode == 1
    assert "remise à son état d'avant" in fini.stdout
    assert venv(banc, "green") == "3.10" and temoin(banc, "green")
    assert not (banc.racine / "opt/exemple/prod-green/.venv.avant-plancher").exists()
    assert not [c for c in banc.commandes() if "reset" in c or c.startswith("uv sync")]
    assert actif(banc) == "blue"
    # la couleur reste le retour arrière : il passe, sur son Python d'avant
    banc.trace.write_text("")
    retour = banc.lancer("deployer.sh", "retour", "prod", "v1.1.0")
    assert retour.returncode == 0, retour.stdout + retour.stderr
    assert actif(banc) == "green" and venv(banc, "green") == "3.10"


def test_sync_en_echec_apres_le_reset_code_et_venv_d_avant_reviennent(role_en_310):
    """Le `git reset --hard` a déjà posé le code du tag quand `uv sync` échoue : la couleur
    reprend sa tête d'avant ET son venv d'avant, ensemble."""
    banc = role_en_310
    plancher(banc, "3.12")
    fini = monter(banc, "v2.0.0", BANC_UV_SYNC_KO="1", BANC_GIT_TETES="1")
    assert fini.returncode == 1
    cmds = banc.commandes()
    arbre = "/opt/exemple/prod-green"
    assert cmds.index("git reset --hard v2.0.0") < cmds.index("uv sync --frozen --quiet") \
        < cmds.index(f"git -C {arbre} reset --quiet --hard {SHA}")
    assert (banc.racine / f"{arbre.lstrip('/')}/.git/tete").read_text().strip() == SHA
    assert venv(banc, "green") == "3.10" and temoin(banc, "green")
    assert not (banc.racine / f"{arbre.lstrip('/')}/.venv/a-moitie-rempli").exists()
    assert not (banc.racine / f"{arbre.lstrip('/')}/.venv.avant-plancher").exists()
    assert f"remise à son état d'avant (code {SHA}, venv Python 3.10)" in fini.stdout
    assert actif(banc) == "blue"


def test_installation_reussie_l_ancien_venv_part(role_en_310):
    banc = role_en_310
    plancher(banc, "3.12")
    assert monter(banc, "v2.0.0", BANC_GIT_TETES="1").returncode == 0
    assert not (banc.racine / "opt/exemple/prod-green/.venv.avant-plancher").exists()
    assert not [c for c in banc.commandes() if c.startswith("git -C") and " reset " in c]


# --- le choix d'interpréteur de l'instance (`python` de la déclaration) -------------
def test_choix_3_12_sur_venv_3_10_migre(role_en_310):
    banc = role_en_310
    decl = choisir(banc, "3.12")
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v2.0.0", declaration=decl)
    assert fini.returncode == 0, fini.stdout + fini.stderr
    cmds = banc.commandes()
    assert "uv python install --quiet 3.12" in cmds
    assert venvs_crees(banc) == [
        "uv venv --quiet --seed --python 3.12 --python-preference only-managed "
        "/opt/exemple/prod-green/.venv"]
    assert venv(banc, "green") == "3.12" and venv(banc, "blue") == "3.10" and temoin(banc, "blue")


def test_choix_3_12_sur_venv_3_12_rien(role_en_310):
    banc = role_en_310
    decl = choisir(banc, "3.12")
    banc.trace.write_text("")
    assert banc.lancer("deployer.sh", "deployer", "prod", "v2.0.0",
                       declaration=decl).returncode == 0     # verte → 3.12
    banc.trace.write_text("")
    assert banc.lancer("deployer.sh", "deployer", "prod", "v2.1.0",
                       declaration=decl).returncode == 0     # bleue → 3.12
    (banc.racine / "opt/exemple/prod-green/.venv/temoin").write_text("green")
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v2.2.0", declaration=decl)
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert venvs_crees(banc) == [] and temoin(banc, "green")


def test_sans_choix_la_regle_du_tag_seule(role_en_310):
    """Venv 3.12 sous `>=3.10`, sans choix : rien ; c'est le choix qui l'aurait ramené."""
    banc = role_en_310
    decl = choisir(banc, "3.12")
    banc.trace.write_text("")
    assert banc.lancer("deployer.sh", "deployer", "prod", "v2.0.0",
                       declaration=decl).returncode == 0     # verte → 3.12
    assert monter(banc, "v2.1.0").returncode == 0            # bleue 3.10, admise
    (banc.racine / "opt/exemple/prod-green/.venv/temoin").write_text("green")
    fini = monter(banc, "v2.2.0")                            # verte 3.12, admise
    assert fini.returncode == 0 and venvs_crees(banc) == []
    assert venv(banc, "green") == "3.12" and temoin(banc, "green")


def test_choix_que_le_tag_n_admet_pas_refus_avant_toute_action(role_en_310):
    banc = role_en_310
    decl = choisir(banc, "3.9")
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v2.0.0", declaration=decl)
    assert fini.returncode == 1
    assert ("l'instance choisit Python 3.9 (« python » de la déclaration), que le "
            "requires-python de v2.0.0 n'admet pas") in fini.stderr
    cmds = banc.commandes()
    assert not [c for c in cmds if c.startswith(("uv ", "git ", "systemctl ", "useradd"))]
    assert venv(banc, "green") == "3.10" and temoin(banc, "green") and actif(banc) == "blue"


def test_retour_vers_la_couleur_3_10_apres_migration_par_le_choix(role_en_310):
    banc = role_en_310
    decl = choisir(banc, "3.12")
    banc.trace.write_text("")
    assert banc.lancer("deployer.sh", "deployer", "prod", "v2.0.0",
                       declaration=decl).returncode == 0
    assert actif(banc) == "green" and venv(banc, "green") == "3.12"
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v2.0.0", declaration=decl)
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert actif(banc) == "blue"
    assert venv(banc, "blue") == "3.10" and temoin(banc, "blue")
    assert venvs_crees(banc) == []
    assert not [c for c in banc.commandes() if "reset" in c]
