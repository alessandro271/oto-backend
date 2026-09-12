"""La consigne qu'une campagne hébergée commande au worker.

Le mode direct remplit ses marqueurs dans `oto_runner/declaration.py` ; le
chemin hébergé les remplit ici. Le 12/09/2026, `{date_du_jour}` n'existait que
du côté direct : les six passes d'une chaîne d'enrichissement l'emploient, et
une campagne les aurait servies avec le marqueur en littéral.
"""
from __future__ import annotations

from datetime import datetime, timezone

from oto_mcp import runner_consigne as C

CAMPAGNE = {"input": "Tableau {namespace}, lignes {filter}, fiche du {date_du_jour}.",
            "namespace": "tableau-editeurs", "row_filter": {"statut": "à enrichir"}}


def test_les_trois_marqueurs_sont_remplis():
    texte = C.composer(CAMPAGNE, maintenant=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    assert texte == ('Tableau tableau-editeurs, lignes {"statut": "à enrichir"}, '
                     "fiche du 12/09/2026.")


def test_le_jour_s_entend_a_PARIS_pas_a_l_heure_du_serveur():
    """23:30 UTC le 11/09, c'est déjà le 12/09 à Paris. Le serveur tourne en UTC :
    sans le fuseau, une consigne partirait datée de la veille pendant deux heures."""
    texte = C.composer({"input": "{date_du_jour}"},
                       maintenant=datetime(2026, 9, 11, 23, 30, tzinfo=timezone.utc))
    assert texte == "12/09/2026"


def test_le_format_est_celui_du_mode_direct():
    """Une même procédure déclarée en local ou en campagne reçoit le même texte :
    `oto_runner/declaration.py` écrit `%d/%m/%Y`."""
    assert C.FORMAT_DATE == "%d/%m/%Y"


def test_une_consigne_sans_marqueur_passe_telle_quelle():
    assert C.composer({"input": "Rien à remplir."}) == "Rien à remplir."
    assert C.composer({}) == ""
