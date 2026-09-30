"""Le déploiement d'une instance cible de bout en bout, en simulation (#967) : la porte
(commande forcée) → le `deploy/` du tag → l'amorce → la bibliothèque bleu/vert → la
maintenance. Banc : `_banc_cible.py` (root simulé, racine jetable, doublures)."""
from __future__ import annotations

import subprocess

import pytest

from _banc_cible import DECLARATION, DEPOT, SHA, Banc


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
# Frapper à la porte par le chemin CHOISI (`ACCES` : tunnel | ssh), exécuté pour de vrai
# avec des doublures qui journalisent (`ssh`, `cloudflared`, `sudo`, `curl`) :
# - accès absent ou inconnu : refus nommé, rien n'est appelé ; aucun repli d'un mode sur
#   l'autre ;
# - `tunnel` exige le jeton de service Access et passe par `cloudflared` ;
# - `ssh` va droit au `:22` de la machine, sans `cloudflared` ni jeton, sans mandataire ;
# - dans les deux : clé d'hôte ÉPINGLÉE (seul le known_hosts de la cible compte), même
#   clé, même commande `<action> <rôle> <tag>`, la déclaration sur l'entrée standard.
APPELER = DEPOT / "deploy" / "cible" / "appeler.sh"
_ENV = {
    "CIBLE_SSH_HOTE": "machine.exemple.test",
    "CIBLE_SSH_UTILISATEUR": "porte-exemple",
    "CIBLE_SSH_KNOWN_HOSTS": "machine.exemple.test ssh-ed25519 AAAAC3Nz",
    "CIBLE_SSH_CLE": "-----BEGIN OPENSSH PRIVATE KEY-----\nfausse\n-----END OPENSSH PRIVATE KEY-----",
    "CIBLE_DECLARATION": '{"instance": "exemple"}',
}
_JETON = {"TUNNEL_SERVICE_TOKEN_ID": "id-exemple", "TUNNEL_SERVICE_TOKEN_SECRET": "secret-exemple"}


def _appeler(tmp_path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, list[str], str]:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    trace, entree = tmp_path / "trace", tmp_path / "entree"
    doublures = {
        # ssh : ses arguments, et ce qu'il reçoit sur l'entrée standard ; il relit aussi
        # le known_hosts épinglé qu'on lui désigne (preuve qu'il existe au moment de l'appel).
        "ssh": f'''for a in "$@"; do echo "ssh $a" >> "{trace}"; done
cat > "{entree}"
prec=""; for a in "$@"; do
  case "$prec$a" in -oUserKnownHostsFile=*) cat "${{a#UserKnownHostsFile=}}" > "{tmp_path}/hotes-lus" ;; esac
  prec="$a"; done''',
        "cloudflared": f'echo "cloudflared $*" >> "{trace}"',
        "sudo": f'echo "sudo $*" >> "{trace}"',
        "curl": f'echo "curl $*" >> "{trace}"',
    }
    for nom, corps in doublures.items():
        (bin_ / nom).write_text(f"#!/bin/bash\n{corps}\n")
        (bin_ / nom).chmod(0o755)
    fini = subprocess.run(["/bin/bash", str(APPELER), "deployer", "preprod", "v1.2.3"],
                          env={"PATH": f"{bin_}:/usr/bin:/bin", **env},
                          capture_output=True, text=True, timeout=30)
    lignes = trace.read_text().splitlines() if trace.exists() else []
    return fini, lignes, entree.read_text() if entree.exists() else ""


def _args_ssh(lignes: list[str]) -> list[str]:
    return [l[len("ssh "):] for l in lignes if l.startswith("ssh ")]


def _options(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-o"]


def _commun(tmp_path, args: list[str], entree: str) -> None:
    """Ce qui ne change pas d'un accès à l'autre."""
    opts = _options(args)
    assert "StrictHostKeyChecking=yes" in opts
    assert "GlobalKnownHostsFile=/dev/null" in opts
    assert "BatchMode=yes" in opts and "IdentitiesOnly=yes" in opts
    assert (tmp_path / "hotes-lus").read_text() == _ENV["CIBLE_SSH_KNOWN_HOSTS"] + "\n"
    assert args[-2:] == ["porte-exemple@machine.exemple.test", "deployer preprod v1.2.3"]
    assert entree == _ENV["CIBLE_DECLARATION"]


def test_acces_ssh_va_droit_au_22_epingle_sans_cloudflared(tmp_path):
    fini, lignes, entree = _appeler(tmp_path, {**_ENV, "ACCES": "ssh"})
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert not [l for l in lignes if not l.startswith("ssh ")], lignes   # ni cloudflared, ni apt
    args = _args_ssh(lignes)
    _commun(tmp_path, args, entree)
    assert args[args.index("-p") + 1] == "22"
    opts = _options(args)
    assert "ProxyCommand=none" in opts
    assert not any("cloudflared" in a for a in args)
    assert "::warning" not in fini.stdout


def test_acces_ssh_signale_un_jeton_access_inutilise(tmp_path):
    fini, lignes, _ = _appeler(tmp_path, {**_ENV, **_JETON, "ACCES": "ssh"})
    assert fini.returncode == 0, fini.stdout + fini.stderr
    assert "Jeton Access inutilisé" in fini.stdout
    assert "ProxyCommand=none" in _options(_args_ssh(lignes))


def test_acces_tunnel_passe_par_cloudflared(tmp_path):
    fini, lignes, entree = _appeler(tmp_path, {**_ENV, **_JETON, "ACCES": "tunnel"})
    assert fini.returncode == 0, fini.stdout + fini.stderr
    args = _args_ssh(lignes)
    _commun(tmp_path, args, entree)
    assert "ProxyCommand=cloudflared access ssh --hostname %h" in _options(args)
    assert "-p" not in args


@pytest.mark.parametrize("jeton", [{}, {"TUNNEL_SERVICE_TOKEN_ID": "id-exemple"}])
def test_acces_tunnel_sans_jeton_refuse(tmp_path, jeton):
    fini, lignes, _ = _appeler(tmp_path, {**_ENV, **jeton, "ACCES": "tunnel"})
    assert fini.returncode == 1
    assert "Tunnel sans jeton" in fini.stdout and "TUNNEL_SERVICE_TOKEN_SECRET" in fini.stdout
    assert lignes == []                                       # rien n'a été appelé


@pytest.mark.parametrize("acces, titre", [(None, "Accès non choisi"), ("", "Accès non choisi"),
                                          ("direct", "Accès inconnu")])
def test_acces_absent_ou_inconnu_refuse(tmp_path, acces, titre):
    env = {**_ENV, **_JETON}
    if acces is not None:
        env["ACCES"] = acces
    fini, lignes, _ = _appeler(tmp_path, env)
    assert fini.returncode == 1
    assert titre in fini.stdout
    assert lignes == []


@pytest.mark.parametrize("acces", ["ssh", "tunnel"])
@pytest.mark.parametrize("absente", sorted(_ENV))
def test_une_valeur_de_la_cible_absente_refuse_dans_les_deux_acces(tmp_path, acces, absente):
    env = {k: v for k, v in {**_ENV, **_JETON, "ACCES": acces}.items() if k != absente}
    fini, lignes, _ = _appeler(tmp_path, env)
    assert fini.returncode == 1
    assert "Cible non déclarée" in fini.stdout and absente in fini.stdout
    assert lignes == []


@pytest.mark.parametrize("acces", ["ssh", "tunnel"])
def test_les_messages_ne_citent_pas_la_cible(tmp_path, acces):
    env = {**_ENV, "ACCES": acces}
    fini, _, _ = _appeler(tmp_path, env)
    assert "exemple" not in fini.stdout + fini.stderr


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
    assert "actions: read" in fini.stdout


def test_protection_un_depot_mal_forme_refuse(tmp_path):
    """Le dépôt du run est vérifié avant d'être mis dans une URL d'API."""
    fini = subprocess.run(["/bin/bash", str(DEPOT / "deploy/cible/protection.sh"),
                           "proprio/depot/../autre", "exemple"],
                          env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                          timeout=30)
    assert fini.returncode == 1 and "owner/repo" in fini.stdout
