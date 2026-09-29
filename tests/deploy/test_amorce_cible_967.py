"""L'amorce d'une instance cible (`deploy/cible/amorcer.sh`, #967) fait naître un rôle
sur une machine nue, n'y refait rien la seconde fois, et refuse en nommant ce que le
socle doit poser. Banc : `_banc_cible.py` (root simulé, racine jetable)."""
from __future__ import annotations

import json
import re

import pytest

from _banc_cible import DECLARATION, Banc, TRONC


@pytest.fixture
def banc(tmp_path):
    return Banc(tmp_path)


def test_naissance_d_un_role_sur_machine_nue(banc):
    fini = banc.lancer("amorcer.sh", "prod")
    assert fini.returncode == 0, fini.stderr
    cmds = banc.commandes()
    assert "useradd --system --user-group --no-create-home --home-dir /nonexistent " \
           "--shell /usr/sbin/nologin oto-exemple" in cmds
    for c in ("blue", "green"):
        assert f"git clone --quiet {TRONC} /opt/exemple/prod-{c}" in cmds
        assert (f"uv venv --quiet --seed --python 3.10 --python-preference only-managed "
                f"/opt/exemple/prod-{c}/.venv") in cmds
    assert "uv python install --quiet 3.10" in cmds
    assert banc.lire("/etc/exemple/prod/active") == "blue\n"
    # l'unité : rendue, non-root, lanceur versionné de l'arbre, clé par LoadCredential
    unite = banc.lire("/etc/systemd/system/exemple-prod@.service")
    assert not re.search(r"@[A-Z_]+@", unite)
    assert "User=oto-exemple" in unite and "User=root" not in unite
    assert f"LoadCredential=scw:{banc.racine}/etc/exemple/scw.key" in unite
    assert (f"ExecStart={banc.racine}/opt/exemple/prod-%i/.venv/bin/python "
            f"{banc.racine}/opt/exemple/prod-%i/deploy/lanceur_secrets.py") in unite
    # l'amont initial pointe la couleur de naissance
    amont = banc.lire("/etc/caddy/upstream-exemple-prod.conf")
    assert "(exemple_prod_upstream)" in amont and "reverse_proxy 127.0.0.1:9203" in amont
    assert "(exemple_prod_ask)" not in amont
    # les fichiers d'environnement : non-secret seulement, lisibles de root seul
    app = banc.lire("/etc/exemple/prod/app.env")
    assert "OTO_BRAND_NAME='Exemple'" in app and "DATABASE_URL" not in app
    assert "OTO_SECRETS_CHEMIN=/prod" in banc.lire("/etc/exemple/prod/lanceur.env")
    assert banc.lire("/etc/exemple/prod/port-green.env").endswith("PORT=9207\n")
    assert banc.mode("/etc/exemple/prod/app.env") == "0o600"
    assert banc.mode("/etc/exemple/prod") == "0o700"
    assert banc.mode("/usr/local/lib/exemple/oto-mcp-drain.sh") == "0o755"
    assert "systemctl daemon-reload" in cmds


def test_la_seconde_fois_rien_ne_renait(banc):
    assert banc.lancer("amorcer.sh", "prod").returncode == 0
    banc.trace.write_text("")
    (banc.racine / "etc/exemple/prod/active").write_text("green\n")
    fini = banc.lancer("amorcer.sh", "prod")
    assert fini.returncode == 0, fini.stderr
    cmds = banc.commandes()
    assert not [c for c in cmds if c.startswith(("useradd", "git clone", "uv venv"))]
    assert banc.lire("/etc/exemple/prod/active") == "green\n"     # jamais réécrit


def test_deux_roles_cohabitent(banc):
    assert banc.lancer("amorcer.sh", "preprod").returncode == 0
    assert banc.lancer("amorcer.sh", "prod").returncode == 0
    assert "reverse_proxy 127.0.0.1:9205" in banc.lire("/etc/caddy/upstream-exemple-preprod.conf")
    assert "OTO_SECRETS_CHEMIN=/preprod" in banc.lire("/etc/exemple/preprod/lanceur.env")


def test_sans_cle_d_api_refuse(banc):
    banc.cle.unlink()
    fini = banc.lancer("amorcer.sh", "prod")
    assert fini.returncode == 1 and "clé d'API du Secret Manager absente" in fini.stderr


def test_une_cle_lisible_par_d_autres_refuse(banc):
    banc.cle.chmod(0o644)
    fini = banc.lancer("amorcer.sh", "prod")
    assert fini.returncode == 1 and "0600" in fini.stderr


def test_un_caddyfile_qui_n_importe_pas_l_amont_dit_quoi_ajouter(banc):
    banc.caddyfile(avec_import=False)
    fini = banc.lancer("amorcer.sh", "prod")
    assert fini.returncode == 1
    assert "import /etc/caddy/upstream-exemple-prod.conf" in fini.stderr.replace(str(banc.racine), "")
    assert "import exemple_prod_upstream" in fini.stderr


def test_un_arbre_qui_suit_un_autre_depot_refuse(banc):
    assert banc.lancer("amorcer.sh", "prod").returncode == 0
    (banc.racine / "opt/exemple/prod-green/.git/origine").write_text("https://ailleurs.test/x.git\n")
    fini = banc.lancer("amorcer.sh", "prod")
    assert fini.returncode == 1 and "pas le tronc" in fini.stderr


def test_un_venv_hors_du_plancher_refuse(banc):
    fini = banc.lancer("amorcer.sh", "prod", BANC_PYV="3.12")
    assert fini.returncode == 1 and "le tag exige 3.10" in fini.stderr


def test_sans_depot_du_tronc_refuse(banc):
    fini = banc.lancer("amorcer.sh", "prod", OTO_CIBLE_DEPOT="")
    assert fini.returncode == 1 and "OTO_CIBLE_DEPOT" in fini.stderr


def test_une_declaration_refusee_n_amorce_rien(banc, tmp_path):
    doc = json.loads(DECLARATION.read_text())
    doc["roles"]["prod"]["env"]["DATABASE_URL"] = "postgres://x"
    mauvaise = tmp_path / "mauvaise.json"
    mauvaise.write_text(json.dumps(doc))
    fini = banc.lancer("amorcer.sh", "prod", declaration=mauvaise)
    assert fini.returncode == 1 and "DATABASE_URL : c'est un secret" in fini.stderr
    assert [c.split()[2] for c in banc.commandes()] == ["variables"]   # rien d'autre


def test_la_maintenance_suit_la_couleur_active(banc):
    assert banc.lancer("amorcer.sh", "prod").returncode == 0
    (banc.racine / "etc/exemple/prod/active").write_text("green\n")
    fini = banc.lancer("amorcer.sh", "prod", "maintenance")
    assert fini.returncode == 0, fini.stderr
    service = banc.lire("/etc/systemd/system/exemple-prod-maintenance.service")
    assert f"{banc.racine}/opt/exemple/prod-green/deploy/lanceur_secrets.py maintenance all" in service
    assert "User=oto-exemple" in service
    assert banc.lire("/etc/systemd/system/exemple-prod-maintenance.timer") == \
        (banc.tag / "deploy/oto-mcp-maintenance.timer").read_text()
    assert "systemctl enable --now exemple-prod-maintenance.timer" in banc.commandes()
