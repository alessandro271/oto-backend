"""Clients HTTP minces vers le service FOD — capacité « urba » (ADR 0028, B3).

Enveloppe réglementaire/territoriale (GPU, Géorisques, QPV, EPFIF, INSEE Mélodi,
IRIS DuckDB) servie par le service FOD dédié — le backend ne l'exécute plus
in-process. Objets proxy répliquant la surface des clients `france_opendata`.

`georisques` est consommé ici (urba) ET par `foncier_icpe` (tools/foncier.py) —
même proxy, fin du partage in-process de B1. Pas de fallback (ADR 0028).
"""
from __future__ import annotations

from typing import Any, Optional

from .http import get as _get, post as _post


class _Gpu:
    def zonage(self, lon: float, lat: float) -> dict[str, Any]:
        return _get("/api/urba/gpu/zonage", {"lon": lon, "lat": lat})


class _Georisques:
    def risques_commune(self, code_insee: str) -> dict[str, Any]:
        return _get(f"/api/urba/georisques/risques/{code_insee}")

    def alea_argiles(self, lon: float, lat: float) -> dict[str, Any]:
        return _get("/api/urba/georisques/argiles", {"lon": lon, "lat": lat})

    def installations_classees(self, siret: Optional[str] = None,
                               code_insee: Optional[str] = None, page: int = 1) -> dict[str, Any]:
        return _get("/api/urba/georisques/icpe",
                    {"siret": siret, "code_insee": code_insee, "page": page})


class _Qpv:
    def by_commune(self, code_insee: str) -> dict[str, Any]:
        return _get(f"/api/urba/qpv/{code_insee}")

    def near_point(self, lon: float, lat: float, radius_m: int = 300) -> dict[str, Any]:
        return _get("/api/urba/qpv/near", {"lon": lon, "lat": lat, "radius_m": radius_m})


class _Epfif:
    def lookup(self, code_insee: str) -> dict[str, Any]:
        return _get(f"/api/urba/epfif/{code_insee}")


class _InseeMelodi:
    def _block(self, code_insee: str, block: str) -> dict[str, Any]:
        return _get(f"/api/urba/insee/{code_insee}/{block}")

    def population(self, code_insee: str) -> dict[str, Any]:
        return self._block(code_insee, "population")

    def familles(self, code_insee: str) -> dict[str, Any]:
        return self._block(code_insee, "familles")

    def personnes_seules(self, code_insee: str) -> dict[str, Any]:
        return self._block(code_insee, "personnes_seules")

    def revenus(self, code_insee: str) -> dict[str, Any]:
        return self._block(code_insee, "revenus")

    def logement(self, code_insee: str) -> dict[str, Any]:
        return self._block(code_insee, "logement")


class _Iris:
    def by_iris(self, code: str) -> Optional[dict[str, Any]]:
        return _get(f"/api/urba/iris/by_iris/{code}")

    def by_commune(self, code: str) -> dict[str, Any]:
        return _get(f"/api/urba/iris/by_commune/{code}")


class _Elus:
    def maires(self, code_commune: Optional[str] = None, departement: Optional[str] = None,
               limit: int = 100) -> dict[str, Any]:
        return _get("/api/urba/elus/maires",
                    {"code_commune": code_commune, "departement": departement, "limit": limit})

    def presidents_epci(self, siren: Optional[str] = None, departement: Optional[str] = None,
                        limit: int = 100) -> dict[str, Any]:
        return _get("/api/urba/elus/presidents-epci",
                    {"siren": siren, "departement": departement, "limit": limit})


class _Annuaire:
    def services(self, siren: Optional[str] = None, code_commune: Optional[str] = None,
                 type_service: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        return _get("/api/urba/annuaire",
                    {"siren": siren, "code_commune": code_commune,
                     "type_service": type_service, "limit": limit})


gpu = _Gpu()
georisques = _Georisques()
qpv = _Qpv()
epfif = _Epfif()
insee = _InseeMelodi()
iris = _Iris()
elus = _Elus()
annuaire = _Annuaire()
