"""Les cinq outils de liste arrivés le 11/09 déclarent ce que leur défaut retire.

`tests/test_sorties_listes_projetees.py` refuse un outil NEUF qui pagine sans
projeter : `foncier_proprietaire`, `foncier_emissions`, `urba_elus`, `urba_annuaire`
et `fr_tenders_awarded` y tombaient tous les cinq. Le cliquet ne juge que la FORME
(un paramètre, un appel) ; ce banc juge ce que l'outil REND.

- `foncier_proprietaire` : chaque enregistrement portait `raw`, la ligne BDNB dont
  toutes les colonnes sont déjà rendues, remises en forme, à côté. Duplication pure :
  le défaut la retire, `full=True` la rend ;
- les quatre autres n'ont rien de dupliqué — leur enregistrement est déjà une vue
  choisie par le client de la source. Ils offrent `fields` : omis, rien ne bouge ;
  posé, les enregistrements se resserrent et l'enveloppe reste.

Doublures au niveau des proxies FOD, comme `test_foncier_op_dispatch` : l'outil lit
le proxy à l'appel ou à l'enregistrement, on le remplace avant d'enregistrer.
"""
from __future__ import annotations

import asyncio
import copy

import pytest


def _outil(module, nom):
    from fastmcp import FastMCP

    m = FastMCP("banc-listes-neuves")
    module.register(m)
    return asyncio.run(m.get_tool(nom)).fn


class _Proxy:
    """Rend une copie du payload donné, quelle que soit la méthode appelée."""

    def __init__(self, payload):
        self._payload = payload

    def __getattr__(self, _nom):
        return lambda *a, **k: copy.deepcopy(self._payload)


_BATIMENT = {
    "ref_key": "bdnb-bg-1", "batiment_groupe_id": "bdnb-bg-1", "code_commune": "59350",
    "proprietaire": {"siren": "123456789", "denomination": "SCI X"},
    "bati": {"emprise_m2": 1200.0}, "energie": {"conso_pro_elec_kwh": 1.0},
    "raw": {"batiment_groupe_id": "bdnb-bg-1", "siren": "123456789",
            "surface_emprise_sol": 1200},
}
_BDNB = {"total": 1, "requetes": 1, "tronque": False,
         "couverture_partielle": "personnes morales seules", "signaux": [_BATIMENT]}


def test_le_proprietaire_ne_rend_plus_sa_ligne_brute_par_defaut(monkeypatch):
    from oto_mcp.fod import foncier as fod_foncier
    from oto_mcp.tools import foncier as F

    monkeypatch.setattr(fod_foncier, "bdnb", _Proxy(_BDNB))
    out = _outil(F, "foncier_proprietaire")(code_commune="59350")
    assert "raw" not in out["signaux"][0]
    # Seule la copie brute part : le reste de l'enregistrement et l'enveloppe restent.
    attendu = {k: v for k, v in _BATIMENT.items() if k != "raw"}
    assert out["signaux"][0] == attendu
    assert {k: v for k, v in out.items() if k != "signaux"} == \
        {k: v for k, v in _BDNB.items() if k != "signaux"}


def test_le_proprietaire_rend_sa_ligne_brute_sur_full(monkeypatch):
    from oto_mcp.fod import foncier as fod_foncier
    from oto_mcp.tools import foncier as F

    monkeypatch.setattr(fod_foncier, "bdnb", _Proxy(_BDNB))
    assert _outil(F, "foncier_proprietaire")(code_commune="59350", full=True) == _BDNB


def _cas():
    """(module d'outils, nom, comment poser la doublure, payload) — un par outil."""
    from oto_mcp.fod import foncier as fod_foncier
    from oto_mcp.fod import fr as fod_fr
    from oto_mcp.fod import urba as fod_urba
    from oto_mcp.tools import foncier as F
    from oto_mcp.tools import fr as FR
    from oto_mcp.tools import urba as U

    irep = {"annee": 2024, "total": 1, "tronque": False, "sous_seuil": 0,
            "signaux": [{"siret": "1", "nom": "Usine", "quantite": 5.0, "unite": "kg"}]}
    decp = {"total": 1, "rendus": 1, "tronque": False,
            "signaux": [{"objet": "Toiture", "montant": 10, "titulaires": []}]}
    elus = {"total": 1, "tronque": False,
            "signaux": [{"nom": "Durand", "prenom": "Ana", "code_commune": "59350"}]}
    annuaire = {"total": 1, "signaux": [{"nom": "Mairie", "courriel": "m@x.fr",
                                         "responsables": []}]}
    return [
        (F, "foncier_emissions",
         lambda mp, p: mp.setattr(fod_foncier, "irep", _Proxy(p)), irep, "siret"),
        (FR, "fr_tenders_awarded",
         lambda mp, p: mp.setattr(fod_fr, "search_decp",
                                  lambda **k: copy.deepcopy(p)), decp, "objet"),
        (U, "urba_elus",
         lambda mp, p: mp.setattr(fod_urba, "elus", _Proxy(p)), elus, "nom"),
        (U, "urba_annuaire",
         lambda mp, p: mp.setattr(fod_urba, "annuaire", _Proxy(p)), annuaire, "nom"),
    ]


@pytest.mark.parametrize("i", range(4))
def test_sans_fields_rien_ne_bouge(monkeypatch, i):
    module, nom, poser, payload, _ = _cas()[i]
    poser(monkeypatch, payload)
    assert _outil(module, nom)() == payload


@pytest.mark.parametrize("i", range(4))
def test_fields_resserre_les_enregistrements_et_garde_l_enveloppe(monkeypatch, i):
    module, nom, poser, payload, cle = _cas()[i]
    poser(monkeypatch, payload)
    out = _outil(module, nom)(fields=[cle])
    assert out["signaux"] == [{cle: payload["signaux"][0][cle]}]
    assert {k: v for k, v in out.items() if k != "signaux"} == \
        {k: v for k, v in payload.items() if k != "signaux"}
