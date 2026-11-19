"""Le bleu/vert ne coupe jamais une requête en cours (19/11/2026).

Trois défauts corrigés, chacun gardé ici :

- **deux montées rapprochées** : la seconde arrêtait sec la vidange de la première et sa
  couleur. Elle ATTEND désormais la fin de la vidange, bornée ; borne atteinte, elle
  refuse en le nommant, sans rien arrêter ;
- **`bg_abort` après une rebascule** : la couleur qui a servi (jusqu'à ~20 s de trafic)
  était arrêtée net ; elle est maintenant VIDÉE ;
- **le plafond** : 120 s coupaient les appels plus longs. Relevé du journal d'appels de
  la production sur 30 jours : le plus long appel abouti dure 190 s. Plafond de vidange
  et arrêt gracieux d'uvicorn valent 240 s, sous le `TimeoutStopSec` des unités de la
  couleur (5 min, versionnées par l'exploitation) — avec une marge pour fermer le
  processus, et sans dépasser 270 s (une instance tierce rend son `TimeoutStopSec`
  `BG_DRAIN_MAX + 30`).
"""
from __future__ import annotations

import pathlib
import re

import _banc_bleu_vert as banc

RACINE = banc.DEPLOY_DU_DEPOT.parent
UNITE_ARRET_S = 300          # TimeoutStopSec=5min des unités oto-mcp@ et oto-mcp-canari@
MARGE_FERMETURE_S = 30       # le temps de fermer le processus après les requêtes


def _arret_gracieux() -> int:
    texte = (RACINE / "oto_mcp/bornes_processus.py").read_text(encoding="utf-8")
    return int(re.search(r"(?m)^ARRET_GRACIEUX_S = (\d+)$", texte).group(1))


def _plafond(wrapper: str) -> int:
    texte = (banc.DEPLOY_DU_DEPOT / wrapper).read_text(encoding="utf-8")
    return int(re.search(r"(?m)^BG_DRAIN_MAX=(\d+)$", texte).group(1))


def _commandes(trace: str) -> list[str]:
    return trace.split("### commandes\n")[1].split("### sortie\n")[0].splitlines()


def test_plafond_de_vidange_egal_a_l_arret_gracieux_et_sous_l_unite():
    gracieux = _arret_gracieux()
    for wrapper in ("oto-backend.sh", "oto-backend-canari.sh"):
        assert _plafond(wrapper) == gracieux, wrapper
    assert gracieux + 2 * MARGE_FERMETURE_S <= UNITE_ARRET_S
    assert gracieux <= 270


def test_uvicorn_recoit_l_arret_gracieux():
    texte = (RACINE / "oto_mcp/server.py").read_text(encoding="utf-8")
    assert "timeout_graceful_shutdown=bornes_processus.ARRET_GRACIEUX_S" in texte


def test_une_montee_attend_la_vidange_en_cours_sans_la_couper():
    trace = banc.rejouer(banc.DEPLOY_DU_DEPOT, "prod-montee-pendant-vidange")
    commandes = _commandes(trace)
    assert "### code de sortie : 0\n" in trace
    assert not any(c.startswith("systemctl stop oto-mcp-drain") for c in commandes)
    # La couleur de la vidange n'est touchée qu'après la dernière question à la vidange.
    derniere_question = max(i for i, c in enumerate(commandes)
                            if c.startswith("systemctl is-active --quiet oto-mcp-drain"))
    arret = commandes.index("systemctl stop oto-mcp@green")
    assert arret > derniere_question
    assert "on attend sa fin" in trace and "terminée" in trace


def test_une_vidange_sans_fin_refuse_la_montee_en_le_nommant_sans_rien_arreter():
    trace = banc.rejouer(banc.DEPLOY_DU_DEPOT, "prod-vidange-sans-fin")
    commandes = _commandes(trace)
    assert "### code de sortie : 1\n" in trace
    assert "montée REFUSÉE" in trace and "oto-mcp-drain-prod" in trace
    assert not any(c.startswith(("systemctl stop", "git ", "uv ", "systemd-run"))
                   for c in commandes)
    # Bornée : la plus longue vie d'une vidange (plafond, arrêt gracieux, fermeture).
    attentes = sum(1 for c in commandes if c.startswith("sleep "))
    assert attentes * 5 >= 2 * _plafond("oto-backend.sh") + MARGE_FERMETURE_S
    assert "--- /etc/oto-mcp/active-prod\nblue\n" in trace


def test_une_couleur_qui_a_servi_est_videe_avant_d_etre_arretee():
    for scenario in ("prod-public-ko-rebascule", "prod-certificat-jamais-pret"):
        trace = banc.rejouer(banc.DEPLOY_DU_DEPOT, scenario)
        commandes = _commandes(trace)
        # Après la première bascule vers green, green n'est plus jamais arrêtée net.
        apres = commandes[commandes.index("systemctl reload caddy"):]
        assert "systemctl stop oto-mcp@green" not in apres, scenario
        assert any(c.startswith("systemd-run ") and "oto-mcp-drain.sh oto-mcp green 9107" in c
                   for c in apres), scenario
        assert "couleur green en vidange (elle a servi)" in trace


def test_une_couleur_qui_n_a_pas_servi_est_arretee_net():
    trace = banc.rejouer(banc.DEPLOY_DU_DEPOT, "prod-couleur-morte")
    assert "systemctl stop oto-mcp@green" in _commandes(trace)
    assert not any(c.startswith("systemd-run ") for c in _commandes(trace))
