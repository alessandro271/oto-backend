"""#1351 (3a) — `fr_stock_enrich` / `fr_stock_search` rendent la raison sociale.

Le stock interrogé est celui des ÉTABLISSEMENTS : son `denomination` est
`denominationUsuelleEtablissement`, presque toujours vide (334 nuls sur 337 SIREN,
~88 % des établissements d'une recherche). La raison sociale vit dans le stock
des unités légales, que FOD sert déjà (`/api/sirene/unites-legales`) mais que ces
outils ne joignaient pas.

Banc : le VRAI outil, client FOD stubé à la forme de ses routes.
"""
from __future__ import annotations

import pytest


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco(a[0]) if a and callable(a[0]) else deco


# `/api/sirene/enrich` : le nom d'usage est vide, comme pour la plupart des sièges.
_SIEGES = {
    "111111111": {"siren": "111111111", "siret": "11111111100015", "street": "1 RUE A",
                  "postal_code": "75008", "city": "PARIS", "naf": "70.10Z",
                  "denomination": None, "status": "active"},
    "222222222": {"siren": "222222222", "siret": "22222222200011", "street": "2 RUE B",
                  "postal_code": "69001", "city": "LYON", "naf": "96.02A",
                  "denomination": None, "status": "active"},
    "333333333": {"siren": "333333333", "siret": "33333333300017", "street": "3 RUE C",
                  "postal_code": "13001", "city": "MARSEILLE", "naf": "47.11F",
                  "denomination": None, "status": "active"},
}
# `/api/sirene/unites-legales` : un SIREN inconnu est ABSENT ; un entrepreneur
# individuel n'a pas de dénomination.
_UNITES = {
    "111111111": {"siren": "111111111", "denomination": "GROUPE EXEMPLE",
                  "categorie_entreprise": "GE"},
    "222222222": {"siren": "222222222", "denomination": None, "nom": "DURAND",
                  "categorie_entreprise": "PME"},
}


@pytest.fixture()
def outils(monkeypatch):
    demandes = []
    from oto_mcp.fod import client
    monkeypatch.setattr(client, "headquarters_addresses",
                        lambda sirens: {s: dict(_SIEGES[s]) for s in sirens if s in _SIEGES})
    monkeypatch.setattr(client, "search",
                        lambda **kw: [dict(v) for v in _SIEGES.values()])

    def _ul(sirens):
        demandes.append(list(sirens))
        return {s: dict(_UNITES[s]) for s in sirens if s in _UNITES}
    monkeypatch.setattr(client, "lookup_unites_legales", _ul)
    from oto_mcp.tools import fr_stock
    reg = _Reg()
    fr_stock.register(reg)
    return reg.tools, demandes


def test_enrich_porte_la_raison_sociale(outils):
    tools, demandes = outils
    res = tools["fr_stock_enrich"](sirens=["111111111", "222222222", "333333333"])
    assert demandes == [["111111111", "222222222", "333333333"]], "une seule jointure pour le lot"
    assert res["111111111"]["raison_sociale"] == "GROUPE EXEMPLE"
    assert res["111111111"]["categorie_entreprise"] == "GE"
    assert res["111111111"]["denomination"] is None, "le nom d'usage reste ce qu'il est"
    assert res["222222222"]["raison_sociale"] is None, "un EI n'a pas de raison sociale"
    assert res["333333333"]["unite_legale"] == "absente"
    assert "raison_sociale" not in res["333333333"]


def test_search_porte_la_raison_sociale(outils):
    tools, _ = outils
    items = tools["fr_stock_search"](departement="75")["items"]
    assert [i.get("raison_sociale") for i in items] == ["GROUPE EXEMPLE", None, None]
    assert items[2]["unite_legale"] == "absente"


def test_aucun_resultat_aucune_jointure(outils, monkeypatch):
    tools, demandes = outils
    from oto_mcp.fod import client
    monkeypatch.setattr(client, "search", lambda **kw: [])
    assert tools["fr_stock_search"](departement="75")["items"] == []
    assert demandes == []
