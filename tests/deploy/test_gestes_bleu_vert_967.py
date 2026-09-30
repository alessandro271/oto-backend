"""Nos deux déploiements font EXACTEMENT les mêmes gestes qu'avant la bibliothèque
générique (#967).

La bibliothèque bleu/vert sert désormais aussi une instance tierce ; nos wrappers y
déclarent explicitement ce qui était codé en dur (verrou, Caddyfile, vidange, mode du
lanceur). Ce test rejoue chaque scénario réel de notre box — bascule dans les deux sens,
retour arrière, préproduction, vidange et chemins d'échec — et compare la trace des
commandes, la sortie et l'état final des fichiers à la référence enregistrée depuis les
scripts d'avant (`tests/deploy/gestes_bleu_vert/`), régénérée au lot 5 pour le seul
passage à `BG_LANCEUR=versionne`. Mécanisme et régénération :
`tests/deploy/_banc_bleu_vert.py`.
"""
from __future__ import annotations

import pytest

import _banc_bleu_vert as banc


@pytest.mark.parametrize("scenario", sorted(banc.SCENARIOS))
def test_memes_gestes_qu_avant(scenario):
    assert banc.rejouer(banc.DEPLOY_DU_DEPOT, scenario) == banc.reference(scenario)


def test_chaque_reference_a_son_scenario():
    """Une référence orpheline serait une preuve qu'on ne rejoue plus."""
    fichiers = {p.stem for p in banc.REFERENCES.glob("*.txt")}
    assert fichiers == set(banc.SCENARIOS)


# --- ce que la bibliothèque refuse, et que l'ancienne supposait -------------------
_DECLARATION = {
    "BG_ENV": "t", "BG_UNIT": "u", "BG_TREE": "/x", "BG_PORT_blue": "1",
    "BG_PORT_green": "2", "BG_UPSTREAM": "/x/amont", "BG_SNIPPET": "s",
    "BG_DOCSHARE_HOST": "h", "BG_ASK": "", "BG_ACTIVE": "/x/active",
    "BG_PUBLIC": "https://h/", "BG_DRAIN_MAX": "1", "BG_LOCK": "/x/verrou",
    "BG_CADDYFILE": "/x/Caddyfile", "BG_DRAIN": "/x/vidange", "BG_DRAIN_UNIT": "v",
    "BG_LANCEUR": "versionne",
}


def _charger(declaration: dict, suite: str = "echo chargee"):
    import subprocess
    affectations = "".join(f"{k}={v!r}\n" for k, v in declaration.items())
    script = affectations + f". {banc.DEPLOY_DU_DEPOT}/oto-mcp-bluegreen.sh\n" + suite + "\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_une_declaration_complete_se_charge():
    fini = _charger(_DECLARATION)
    assert (fini.returncode, fini.stdout.strip()) == (0, "chargee"), fini.stderr


@pytest.mark.parametrize("variable", sorted(_DECLARATION))
def test_une_variable_non_declaree_refuse_le_chargement(variable):
    declaration = {k: v for k, v in _DECLARATION.items() if k != variable}
    fini = _charger(declaration)
    assert fini.returncode == 2
    assert variable in fini.stderr and "non déclarée" in fini.stderr


def test_un_mode_de_lanceur_inconnu_refuse():
    fini = _charger({**_DECLARATION, "BG_LANCEUR": "copie"})
    assert fini.returncode == 2 and "BG_LANCEUR='copie'" in fini.stderr


def test_un_pointeur_de_couleur_absent_est_une_panne_nommee(tmp_path):
    fini = _charger({**_DECLARATION, "BG_ACTIVE": str(tmp_path / "absent")},
                    "bg_active || echo refus")
    assert fini.stdout.strip() == "refus"
    assert "pointeur de couleur" in fini.stderr
