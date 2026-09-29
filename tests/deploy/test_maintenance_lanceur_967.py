"""Les travaux planifiés de notre box passent par le lanceur générique (#967, lot 5c).

La maintenance quotidienne et l'archive du journal lisent la base et le stockage : une
fois les secrets sortis du `.env`, ils n'y ont plus accès qu'en passant par
`deploy/lanceur_secrets.py`. La maintenance est posée par le déploiement prod
(`install_timers`), l'archive à la main.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import _banc_bleu_vert as banc

DEPLOY = banc.DEPLOY_DU_DEPOT


def _unite(nom: str) -> dict[str, list[str]]:
    lignes = (DEPLOY / nom).read_text(encoding="utf-8").splitlines()
    unite: dict[str, list[str]] = {}
    for ligne in lignes:
        m = re.match(r"([A-Za-z]+)=(.*)", ligne)
        if m:
            unite.setdefault(m.group(1), []).append(m.group(2))
    return unite


def test_la_maintenance_demarre_par_le_lanceur_avec_la_cle_d_api_en_credential():
    u = _unite("oto-mcp-maintenance.service")
    assert u["ExecStart"] == [
        "@ARBRE@/.venv/bin/python @ARBRE@/deploy/lanceur_secrets.py maintenance all"]
    assert u["WorkingDirectory"] == ["@ARBRE@"]
    assert u["LoadCredential"] == ["scw:/etc/oto-mcp/scw.key"]
    assert u["EnvironmentFile"] == ["/opt/oto-mcp/.env", "/etc/oto-mcp/lanceur-prod.env"]


def test_l_archive_du_journal_demarre_le_script_versionne_par_le_lanceur():
    u = _unite("oto-journal-archive.service")
    assert u["ExecStart"] == [
        "/opt/oto-mcp/.venv/bin/python /opt/oto-mcp/deploy/lanceur_secrets.py"
        " --script deploy/archive_tool_calls.py"]
    assert u["LoadCredential"] == ["scw:/etc/oto-mcp/scw.key"]
    assert (DEPLOY / "archive_tool_calls.py").is_file()


def test_install_timers_ecrit_l_arbre_de_la_couleur_en_service(tmp_path):
    """Le déploiement pose l'unité avec l'arbre qui vient d'être activé, pas un chemin
    figé qui pourrait porter un tag sans lanceur."""
    racine = tmp_path
    arbre = racine / "opt/oto-mcp-green"
    (arbre / "deploy").mkdir(parents=True)
    (racine / "etc/systemd/system").mkdir(parents=True)
    for u in ("oto-mcp-maintenance.service", "oto-mcp-maintenance.timer"):
        (arbre / "deploy" / u).write_text((DEPLOY / u).read_text(encoding="utf-8"))
    source = (DEPLOY / "oto-backend.sh").read_text(encoding="utf-8")
    fonction = re.search(r"^install_timers\(\) \{.*?^\}", source, re.S | re.M).group(0)
    (racine / "bin").mkdir()
    for nom in ("systemctl",):
        (racine / "bin" / nom).write_text("#!/bin/bash\nexit 0\n")
        (racine / "bin" / nom).chmod(0o755)
    script = banc._reecrire(fonction, str(racine)) + f'\ninstall_timers "{arbre}"\n'
    fini = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": f"{racine}/bin:/usr/bin:/bin"}, timeout=30)
    assert fini.returncode == 0, fini.stderr
    pose = (racine / "etc/systemd/system/oto-mcp-maintenance.service").read_text()
    assert "@ARBRE@" not in pose
    assert f"ExecStart={arbre}/.venv/bin/python {arbre}/deploy/lanceur_secrets.py maintenance all" in pose
    assert f"WorkingDirectory={arbre}\n" in pose
