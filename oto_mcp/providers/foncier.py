"""Déclaration de registre du connecteur `foncier`.

Domicile unique de son entrée : `providers/__init__.py` l'AGRÈGE (il ne la
décrit pas). Cf. `providers/_model.py` pour le contrat de `Connector`.
"""
from __future__ import annotations

from ._model import _c

# foncier / sante : connecteurs open-data déclarés (ADR 0010). Inertes tant
# que non activés en DB (connector_activation) — register_all gate dessus,
# donc absents du seed initial → OFF par défaut (deny-by-default).
CONNECTOR = _c(
    "foncier", ["foncier"], secret_kind="none",
    # « Foncier » seul mentait sur la moitié de la boîte : géocodage, isochrones,
    # permis et consommation électrique ne sont pas du foncier (2026-09-02).
    # Le même écart se rejoue côté usage : la conso élec sert autant un ciblage
    # commercial (les gros consommateurs d'un secteur) que la prospection PV, et
    # personne ne cherche ça dans « Foncier ». D'où la mention explicite ci-dessous
    # — c'est la description, pas le label, qui rend un connecteur trouvable.
    label="Foncier & territoire",
    help="adresses et parcelles, bâti, prix au m² (DVF), DPE, conso élec des sites "
         "(distribution ET transport), risques et ICPE, permis, isochrones — open data",
)

CATEGORY = "Data FR"
PUBLISHER = "État (open data)"
DESCRIPTION = (
    "Les sites français en open data : géocodage BAN, parcelles cadastrales, "
    "bâti, transactions DVF (prix au m², comparables par adresse), risques et "
    "ICPE, DPE, productible solaire — et la consommation électrique annuelle "
    "des sites sur les DEUX étages du réseau, distribution (Enedis) et "
    "transport (RTE), le DPE tertiaire et les bilans GES déclarés — de quoi "
    "bâtir une liste de gros consommateurs d'énergie et la qualifier."
)
LOGO_DOMAIN = "data.gouv.fr"
