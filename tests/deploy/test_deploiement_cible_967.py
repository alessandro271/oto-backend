"""Le déploiement d'une instance cible de bout en bout, en simulation (#967) : la porte
(commande forcée) → le `deploy/` du tag → l'amorce → la bibliothèque bleu/vert → la
maintenance. Banc : `_banc_cible.py` (root simulé, racine jetable, doublures)."""
from __future__ import annotations

import pytest

from _banc_cible import DECLARATION, SHA, Banc


@pytest.fixture
def banc(tmp_path):
    return Banc(tmp_path)


def test_premier_deploiement_fait_naitre_le_role_et_installe_la_verte(banc):
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    cmds = banc.commandes()
    assert "git reset --hard v1.2.3" in cmds
    assert "systemctl start exemple-prod@green" in cmds
    assert "systemctl enable exemple-prod@green" in cmds
    assert any(c.startswith("systemd-run --collect --quiet --unit=exemple-prod-vidange ")
               and "/usr/local/lib/exemple/oto-mcp-drain.sh exemple-prod blue 9203 120" in c
               for c in cmds)
    # aucun lanceur propagé : c'est celui du tag
    assert not [c for c in cmds if c.startswith("cp -a") and "start-encrypted" in c]
    assert banc.lire("/etc/exemple/prod/active") == "green\n"
    assert "reverse_proxy 127.0.0.1:9207" in banc.lire("/etc/caddy/upstream-exemple-prod.conf")
    assert f'"commit": "{SHA}"' in banc.lire("/opt/exemple/prod-green/.oto-deploy.json")
    assert "prod-green/deploy/lanceur_secrets.py maintenance all" in \
        banc.lire("/etc/systemd/system/exemple-prod-maintenance.service")
    assert "=== prod OK : blue -> green" in fini.stdout


def test_le_deploiement_suivant_revient_sur_la_bleue_puis_le_retour_arriere(banc):
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3").returncode == 0
    assert banc.lancer("deployer.sh", "deployer", "prod", "v1.3.0").returncode == 0
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"
    banc.trace.write_text("")
    fini = banc.lancer("deployer.sh", "retour", "prod", "v1.3.0")
    assert fini.returncode == 0, fini.stderr
    assert banc.lire("/etc/exemple/prod/active") == "green\n"
    assert not [c for c in banc.commandes() if c.startswith("git reset")]


def test_une_couleur_morte_ne_bascule_rien(banc):
    fini = banc.lancer("deployer.sh", "deployer", "prod", "v1.2.3", BANC_DEMARRAGE_KO="1")
    assert fini.returncode == 1
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"
    assert "rien n'a basculé" in fini.stdout


def test_la_porte_passe_la_main_au_deploiement_du_tag(banc):
    fini = banc.porte("deployer prod v1.2.3")
    assert fini.returncode == 0, fini.stdout + fini.stderr
    cmds = banc.commandes()
    assert "git clone --quiet --mirror https://github.com/otomata-tech/oto-backend.git " \
           "/var/lib/oto-cible/depot.git" in cmds
    assert f"git -C /var/lib/oto-cible/depot.git merge-base --is-ancestor {SHA} refs/heads/main" in cmds
    # le dépôt vient de la porte, jamais de la déclaration
    assert "git clone --quiet https://github.com/otomata-tech/oto-backend.git /opt/exemple/prod-blue" in cmds
    assert banc.lire("/etc/exemple/prod/active") == "green\n"
    assert not list((banc.racine / "run").iterdir())          # rien ne traîne


@pytest.mark.parametrize("commande, motif", [
    ("deployer prod", "tag invalide"),
    ("deployer prod v1.2", "tag invalide"),
    ("deployer prod v1.2.3;reboot", "tag invalide"),
    ("deployer prod v1.2.3; reboot", "commande trop longue"),
    ("deployer prod v1.2.3 encore", "commande trop longue"),
    ("installer prod v1.2.3", "action inconnue"),
    ("deployer staging v1.2.3", "rôle inconnu"),
    ("", "action inconnue"),
])
def test_la_porte_refuse_ce_qu_elle_ne_connait_pas(banc, commande, motif):
    fini = banc.porte(commande)
    assert fini.returncode == 1 and motif in fini.stderr
    assert banc.commandes() == []


def test_la_porte_refuse_sans_declaration(banc):
    fini = banc.porte("deployer prod v1.2.3", entree="")
    assert fini.returncode == 1 and "aucune déclaration" in fini.stderr


def test_la_porte_refuse_une_declaration_demesuree(banc):
    fini = banc.porte("deployer prod v1.2.3", entree="x" * 70000)
    assert fini.returncode == 1 and "65536 octets" in fini.stderr


def test_la_porte_refuse_un_tag_inconnu(banc):
    fini = banc.porte("deployer prod v9.9.9", BANC_TAG_INCONNU="1")
    assert fini.returncode == 1 and "n'existe pas dans le tronc" in fini.stderr


def test_la_porte_refuse_un_tag_hors_du_tronc(banc):
    fini = banc.porte("deployer prod v1.2.3", BANC_HORS_TRONC="1")
    assert fini.returncode == 1 and "pas sur la branche principale" in fini.stderr
    assert not [c for c in banc.commandes() if "archive" in c]


def test_la_declaration_ne_peut_pas_choisir_le_depot():
    """Le dépôt est écrit dans la porte : la déclaration n'a pas de clé pour le dire."""
    import json
    assert "depot" not in json.loads(DECLARATION.read_text())


# --- côté CI : appeler.sh --------------------------------------------------------
def _appeler(tmp_path, **env):
    import subprocess
    from _banc_cible import DEPOT
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for nom, corps in {"cloudflared": "exit 0",
                       "ssh": 'echo "ssh $*" > "$TRACE"; cat >> "$TRACE"'}.items():
        (bin_ / nom).write_text(f"#!/bin/bash\n{corps}\n")
        (bin_ / nom).chmod(0o755)
    trace = tmp_path / "trace"
    return trace, subprocess.run(
        ["/bin/bash", str(DEPOT / "deploy/cible/appeler.sh"), "deployer", "prod", "v1.2.3"],
        env={"PATH": f"{bin_}:/usr/bin:/bin", "TRACE": str(trace), **env},
        capture_output=True, text=True, timeout=30)


_ACCES = {"CIBLE_SSH_HOTE": "ssh.exemple.test", "CIBLE_SSH_UTILISATEUR": "deploy",
          "CIBLE_SSH_KNOWN_HOSTS": "ssh.exemple.test ssh-ed25519 AAAA",
          "CIBLE_SSH_CLE": "cle-privee", "TUNNEL_SERVICE_TOKEN_ID": "id",
          "TUNNEL_SERVICE_TOKEN_SECRET": "secret", "CIBLE_DECLARATION": '{"instance": "x"}'}


def test_appeler_passe_par_le_tunnel_avec_l_hote_epingle(tmp_path):
    trace, fini = _appeler(tmp_path, **_ACCES)
    assert fini.returncode == 0, fini.stderr
    appel, stdin = trace.read_text().split("\n", 1)
    assert "-o StrictHostKeyChecking=yes" in appel and "-o BatchMode=yes" in appel
    assert "-o ProxyCommand=cloudflared access ssh --hostname %h" in appel
    assert appel.endswith("deploy@ssh.exemple.test deployer prod v1.2.3")
    assert stdin == '{"instance": "x"}'


@pytest.mark.parametrize("absente", sorted(_ACCES))
def test_appeler_sans_une_valeur_de_la_cible_rougit(tmp_path, absente):
    trace, fini = _appeler(tmp_path, **{k: v for k, v in _ACCES.items() if k != absente})
    assert fini.returncode == 1 and absente in fini.stdout
    assert not trace.exists()


# --- côté CI : protection.sh ----------------------------------------------------
def _protection(tmp_path, reponse: str, code: int = 0):
    import subprocess
    from _banc_cible import DEPOT
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "reponse").write_text(reponse)
    (bin_ / "gh").write_text(f'#!/bin/bash\necho "$*" > "{tmp_path}/appel"\n'
                             f'cat "{tmp_path}/reponse"\nexit {code}\n')
    (bin_ / "gh").chmod(0o755)
    return subprocess.run(["/bin/bash", str(DEPOT / "deploy/cible/protection.sh"),
                           "proprio/depot", "exemple"],
                          env={"PATH": f"{bin_}:/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=30)


def test_protection_un_relecteur_requis_laisse_passer(tmp_path):
    reponse = '{"protection_rules": [{"type": "wait_timer"}, {"type": "required_reviewers", "reviewers": [{"type": "User"}]}]}'
    fini = _protection(tmp_path, reponse)
    assert fini.returncode == 0 and "1 relecteur(s) requis" in fini.stdout
    assert (tmp_path / "appel").read_text().strip() == "api repos/proprio/depot/environments/exemple"


@pytest.mark.parametrize("reponse", [
    '{"protection_rules": []}',
    '{"name": "exemple"}',
    '{"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}',
    '{"protection_rules": [{"type": "branch_policy"}]}',
])
def test_protection_sans_relecteur_refuse(tmp_path, reponse):
    fini = _protection(tmp_path, reponse)
    assert fini.returncode == 1 and "n'exige aucun relecteur" in fini.stdout


def test_protection_environnement_absent_refuse(tmp_path):
    fini = _protection(tmp_path, '{"message": "Not Found"}', code=1)
    assert fini.returncode == 1 and "introuvable" in fini.stdout
