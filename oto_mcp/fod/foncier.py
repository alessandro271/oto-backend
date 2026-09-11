"""Clients HTTP minces vers le service FOD — capacité « foncier » (ADR 0028).

Le stock de données de site (géocodage BAN, cadastre IGN, bâti BDTOPO, productible
PVGIS, permis Sit@del, conso Enedis, valorisation DVF+, DPE ADEME) est servi par le
service FOD dédié (box `fod-0`) — le backend ne les exécute plus in-process.

Ce module expose des **objets proxy** (`ban`, `cadastre`, `bdtopo`, `pvgis`,
`enedis`, `odre`, `beges`, `dvf`, `dpe`, `dpe_tertiaire`, `sitadel`) qui **répliquent la surface des clients
`france_opendata`** consommés par `tools/foncier.py` (mêmes noms/signatures/retours)
→ le tool ne change que la SOURCE de ses clients, ses corps restent identiques.

Pas de fallback in-process (ADR 0028) : FOD indisponible ⟹ erreur actionnable.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from .http import get as _get, post as _post

# DVF+ est servi par l'API Cerema `apidf-preprod.cerema.fr` — un hôte de PRÉPRODUCTION
# d'un organisme public, qui est aussi la seule adresse que le Cerema documente (vérifié
# le 2026-09-09 : aucun `apidf.cerema.fr`, NXDOMAIN). Personne ne s'est engagé à le
# tenir, et il tombe : mesuré ce jour, 503 systématique, dont un après 60,2 s de silence.
# Sans borne à nous, l'appel attendait la passerelle (~60 s) pour rendre une panne muette.
# Une lecture qui échoue doit échouer VITE et NOMMER sa cause.
# Le plafond n'est pas calibré sur un appel sain — la source est tombée, on ne peut pas
# le mesurer : à revoir quand elle sera revenue, d'où la variable d'environnement.
_DVF_TIMEOUT_S = float(os.environ.get("FOD_DVF_TIMEOUT_S", "20"))


class _Ban:
    def search(self, adresse: str, limit: int = 5, postcode: Optional[str] = None,
               citycode: Optional[str] = None) -> list[dict[str, Any]]:
        return _post("/api/foncier/ban/search",
                     {"adresse": adresse, "limit": limit, "postcode": postcode, "citycode": citycode})

    def reverse(self, lat: float, lon: float) -> Optional[dict[str, Any]]:
        return _get("/api/foncier/ban/reverse", {"lat": lat, "lon": lon})


class _Cadastre:
    def parcelle_at(self, lat: float, lon: float) -> Optional[dict[str, Any]]:
        return _get("/api/foncier/cadastre/parcelle", {"lat": lat, "lon": lon})


class _BdTopo:
    def bati_parcelle(self, geometry: dict, contenance_m2: Optional[float] = None) -> dict[str, Any]:
        return _post("/api/foncier/bdtopo/bati", {"geometry": geometry, "contenance_m2": contenance_m2})


class _Pvgis:
    def productible(self, lat: float, lon: float, kwc: float) -> Optional[dict[str, Any]]:
        return _get("/api/foncier/pvgis/productible", {"lat": lat, "lon": lon, "kwc": kwc})


class _Ign:
    def isochrone(self, lat: float, lon: float, *, minutes=None, metres=None,
                  profile: str = "pedestrian", direction: str = "departure") -> dict[str, Any]:
        params: dict[str, Any] = {"lat": lat, "lon": lon, "profile": profile,
                                  "direction": direction}
        if minutes is not None:
            params["minutes"] = minutes
        if metres is not None:
            params["metres"] = metres
        return _get("/api/foncier/ign/isochrone", params)


class _Sitadel:
    def search(self, kind: str, communes: Optional[str] = None, dept: Optional[str] = None,
               an_min: Optional[int] = None, an_max: Optional[int] = None,
               siren: Optional[str] = None, siret: Optional[str] = None,
               page: int = 1, page_size: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/sitadel/search",
                     {"kind": kind, "communes": communes, "dept": dept,
                      "an_min": an_min, "an_max": an_max, "siren": siren, "siret": siret,
                      "page": page, "page_size": page_size})


class _Enedis:
    def consommation_par_adresse(self, annee: str, dept: Optional[str] = None,
                                 secteur: Optional[str] = None,
                                 naf2: Optional[Any] = None,
                                 code_commune: Optional[Any] = None,
                                 code_epci: Optional[str] = None,
                                 min_mwh: Optional[float] = None, max_mwh: Optional[float] = None,
                                 limit: int = 200) -> dict[str, Any]:
        return _post("/api/foncier/enedis/conso",
                     {"annee": annee, "dept": dept, "secteur": secteur,
                      "naf2": naf2, "code_commune": code_commune, "code_epci": code_epci,
                      "min_mwh": min_mwh, "max_mwh": max_mwh, "limit": limit})

    def sites_par_adresse(self, annee: str, dept: Optional[str] = None,
                          code_commune: Optional[Any] = None, code_epci: Optional[str] = None,
                          naf2: Optional[Any] = None, secteur: Optional[str] = None,
                          min_mwh: Optional[float] = None, limit: int = -1) -> dict[str, Any]:
        return _post("/api/foncier/enedis/sites",
                     {"annee": annee, "dept": dept, "code_commune": code_commune,
                      "code_epci": code_epci, "naf2": naf2, "secteur": secteur,
                      "min_mwh": min_mwh, "limit": limit})


class _Beges:
    def bilans(self, siren: Optional[str] = None, naf: Optional[str] = None,
               annee: Optional[int] = None, departement: Optional[str] = None,
               obligee: Optional[bool] = None, size: int = 100) -> dict[str, Any]:
        return _post("/api/foncier/beges",
                     {"siren": siren, "naf": naf, "annee": annee,
                      "departement": departement, "obligee": obligee, "size": size})


class _Bdnb:
    def batiments(self, code_commune: Optional[str] = None, siren: Optional[str] = None,
                  batiment_groupe_id: Optional[str] = None, departement: Optional[str] = None,
                  emprise_min: Optional[float] = None, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/bdnb",
                     {"code_commune": code_commune, "siren": siren,
                      "batiment_groupe_id": batiment_groupe_id, "departement": departement,
                      "emprise_min": emprise_min, "limit": limit})


class _DpeTertiaire:
    def diagnostics(self, code_commune: Optional[Any] = None, departement: Optional[str] = None,
                    secteur: Optional[str] = None, etiquette: Optional[Any] = None,
                    surface_min: Optional[float] = None, size: int = 100) -> dict[str, Any]:
        return _post("/api/foncier/dpe/tertiaire",
                     {"code_commune": code_commune, "departement": departement,
                      "secteur": secteur, "etiquette": etiquette,
                      "surface_min": surface_min, "size": size})


class _Odre:
    def consommation_transport(self, annee: Any, dept: Optional[str] = None,
                               code_commune: Optional[Any] = None,
                               min_mwh: Optional[float] = None, site_unique: bool = True,
                               limit: int = -1) -> dict[str, Any]:
        return _post("/api/foncier/odre/conso",
                     {"annee": annee, "dept": dept, "code_commune": code_commune,
                      "min_mwh": min_mwh, "site_unique": site_unique, "limit": limit})

    def annees_disponibles(self) -> dict[str, Any]:
        return _get("/api/foncier/odre/annees", {})


class _Dvf:
    def stats(self, code_commune: str, type_local: Optional[str] = None, years: int = 3) -> dict[str, Any]:
        return _post("/api/foncier/dvf/stats",
                     {"code_commune": code_commune, "type_local": type_local, "years": years},
                     timeout=_DVF_TIMEOUT_S)

    def comparables(self, code_commune: str, type_local: Optional[str] = None,
                    surface_min: Optional[float] = None, surface_max: Optional[float] = None,
                    years: int = 2, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/dvf/comparables",
                     {"code_commune": code_commune, "type_local": type_local,
                      "surface_min": surface_min, "surface_max": surface_max,
                      "years": years, "limit": limit},
                     timeout=_DVF_TIMEOUT_S)

    def comparables_by_address(self, adresse: str, radius_m: int = 500, type_local: Optional[str] = None,
                               surface_min: Optional[float] = None, surface_max: Optional[float] = None,
                               years: int = 3, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/dvf/comparables_by_address",
                     {"adresse": adresse, "radius_m": radius_m, "type_local": type_local,
                      "surface_min": surface_min, "surface_max": surface_max,
                      "years": years, "limit": limit},
                     timeout=_DVF_TIMEOUT_S)


class _Dpe:
    def by_address(self, adresse: str, radius_m: int = 200, type_batiment: Optional[str] = None,
                   etiquette: Optional[str] = None, surface_min: Optional[float] = None,
                   surface_max: Optional[float] = None, limit: int = 50) -> dict[str, Any]:
        return _post("/api/foncier/dpe/by_address",
                     {"adresse": adresse, "radius_m": radius_m, "type_batiment": type_batiment,
                      "etiquette": etiquette, "surface_min": surface_min, "surface_max": surface_max,
                      "limit": limit})

    def stats(self, code_commune: str, type_batiment: Optional[str] = None) -> dict[str, Any]:
        return _post("/api/foncier/dpe/stats",
                     {"code_commune": code_commune, "type_batiment": type_batiment})


ban = _Ban()
cadastre = _Cadastre()
bdtopo = _BdTopo()
pvgis = _Pvgis()
ign = _Ign()
sitadel = _Sitadel()
enedis = _Enedis()
odre = _Odre()
beges = _Beges()
bdnb = _Bdnb()
dpe_tertiaire = _DpeTertiaire()
dvf = _Dvf()
dpe = _Dpe()
