"""Les unités de maintenance et d'archive du journal d'une instance cible suivent la couleur
qui sert, à chaque montée et à chaque retour — ou le déploiement le dit.

- AVANT la bascule, elles sont rendues et validées pour la couleur qui va servir : un
  gabarit qui ne se rend pas, un timer absent du tag arrêtent tout, rien n'a basculé ;
- APRÈS la bascule, leur pose (écriture, rechargement, timers, relecture) fait échouer le
  déploiement si elle échoue : le message nomme l'arbre que chaque unité vise encore, et la
  commande qui les repose.

Banc : `_banc_cible.py` (root simulé, racine jetable, doublures)."""
from __future__ import annotations

import pytest

from _banc_cible import Banc

SERVICES = ("exemple-prod-maintenance.service", "exemple-prod-journal-archive.service")


@pytest.fixture
def banc(tmp_path):
    return Banc(tmp_path)


def _vise(banc, service: str) -> str:
    for ligne in banc.lire(f"/etc/systemd/system/{service}").splitlines():
        if ligne.startswith("WorkingDirectory="):
            return ligne.removeprefix("WorkingDirectory=").replace(str(banc.racine), "")
    raise AssertionError(f"{service} sans WorkingDirectory")


def _install_qui_echoue(banc, motif: str) -> None:
    """Doublure d'`install` : refuse d'écrire une cible qui contient <motif>, pose le reste."""
    f = banc.bin / "install"
    f.write_text('#!/bin/bash\necho "install $*" >> "$BANC_TRACE"\n'
                 f'case "${{!#}}" in *{motif}) echo "install : écriture refusée" >&2; exit 1 ;; esac\n'
                 'exec /usr/bin/install "$@"\n')
    f.chmod(0o755)


def _rien_n_a_bascule(banc, fini) -> None:
    assert fini.returncode == 1, fini.stdout + fini.stderr
    assert "rien n'a basculé" in fini.stderr
    cmds = banc.commandes()
    assert not [c for c in cmds if c.startswith("systemctl start ")]
    assert not [c for c in cmds if c.startswith("git reset")]
    assert not [c for c in cmds if c.startswith("caddy ")]


def test_cas_nominal_les_unites_suivent_montees_et_retour(banc):
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert "unités rendues et valides pour" in fini.stdout
    assert "=== prod OK : blue -> green" in fini.stdout
    for s in SERVICES:
        assert _vise(banc, s) == "/opt/exemple/prod-green"
    cmds = banc.commandes()
    assert "systemctl enable --now exemple-prod-maintenance.timer" in cmds
    assert "systemctl enable --now exemple-prod-journal-archive.timer" in cmds
    assert fini.stderr.count("ATTENTION") == 0 and "ÉCHEC" not in fini.stderr

    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.3.0").returncode == 0
    for s in SERVICES:
        assert _vise(banc, s) == "/opt/exemple/prod-blue"

    fini = banc.lancer("deployer.sh", "retour", "prod", "v1.3.0")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert banc.lire("/etc/exemple/prod/active") == "green\n"
    for s in SERVICES:
        assert _vise(banc, s) == "/opt/exemple/prod-green"


def test_un_gabarit_qui_ne_se_rend_pas_arrete_la_montee_avant_la_bascule(banc):
    gabarit = banc.tag / "deploy/cible/journal-archive.service"
    gabarit.write_text(gabarit.read_text() + "Environment=X=@INCONNUE@\n")
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3")
    _rien_n_a_bascule(banc, fini)
    assert "clé non rendue (@INCONNUE@)" in fini.stderr
    assert "ne se rendent pas pour green" in fini.stderr
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"
    for s in SERVICES:
        assert not (banc.racine / "etc/systemd/system" / s).exists()


def test_un_timer_absent_du_tag_arrete_la_montee_avant_la_bascule(banc):
    (banc.tag / "deploy/oto-mcp-maintenance.timer").unlink()
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3")
    _rien_n_a_bascule(banc, fini)
    assert "oto-mcp-maintenance.timer absent du tag" in fini.stderr
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"


def test_le_retour_s_arrete_aussi_avant_la_bascule(banc):
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3").returncode == 0
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.3.0").returncode == 0
    gabarit = banc.tag / "deploy/cible/maintenance.service"
    gabarit.write_text(gabarit.read_text() + "Environment=X=@INCONNUE@\n")
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v1.3.0")
    _rien_n_a_bascule(banc, fini)
    assert "ne se rendent pas pour green" in fini.stderr
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"
    for s in SERVICES:
        assert _vise(banc, s) == "/opt/exemple/prod-blue"


def test_une_pose_en_echec_apres_la_bascule_fait_echouer_le_deploiement(banc):
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3").returncode == 0
    _install_qui_echoue(banc, "journal-archive.service")
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.3.0")
    assert fini.returncode == 1, fini.stdout + fini.stderr
    sortie = fini.stderr.replace(str(banc.racine), "")
    # Le service a basculé : la bascule ne se défait pas pour une maintenance.
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"
    assert "=== prod OK : green -> blue" in fini.stdout
    assert "écriture de /etc/systemd/system/exemple-prod-journal-archive.service en échec" in sortie
    assert "déploiement : ÉCHEC — prod a basculé et est servi par blue (/opt/exemple/prod-blue)" in sortie
    assert "exemple-prod-maintenance.service vise : /opt/exemple/prod-blue (blue)" in sortie
    assert "exemple-prod-journal-archive.service vise : /opt/exemple/prod-green (green)" in sortie
    assert "bash /opt/exemple/prod-blue/deploy/cible/amorcer.sh <déclaration.json> prod maintenance" in sortie
    # Les timers ne sont pas activés sur une pose à moitié faite.
    assert not [c for c in banc.commandes() if c.startswith("systemctl enable --now")]
