"""#1351 (2b) — `fr_get` ne laisse plus lire « actif » une société liquidée.

Le répertoire SIRENE n'enregistre pas toujours la cessation : pour des sociétés
dont la liquidation est clôturée pour insuffisance d'actif depuis des années,
`identity.etat_administratif` vaut « A » alors que `recent_events` du même appel
porte le jugement de clôture (huit cas mesurés le même jour). La donnée source
est fausse ; la réponse rend désormais la contradiction visible.

Banc : le VRAI `fr_get`, avis BODACC à la forme servie par FOD (`jugement` décodé).
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


def _collective(id_, parution, nature, famille="Jugement de clôture"):
    return {"id": id_, "dateparution": parution, "familleavis": "collective",
            "familleavis_lib": "Procédures collectives",
            "jugement": {"type": "initial", "famille": famille, "nature": nature,
                         "date": parution}}


_CLOTURE_LJ = _collective("A201900010001", "2019-05-14",
                          "Jugement de clôture pour insuffisance d'actif")
_OUVERTURE_LJ = _collective("A201700010001", "2017-02-01",
                            "Jugement d'ouverture de liquidation judiciaire",
                            famille="Jugement d'ouverture")
_CLOTURE_SAUVEGARDE = _collective("A202200010001", "2022-03-01",
                                  "Jugement de clôture de la procédure de sauvegarde")


@pytest.fixture()
def fr_get(monkeypatch):
    def _make(etat, events):
        class _E:
            def get_by_siren(self, siren):
                return {"siren": siren, "nom_complet": "EXEMPLE",
                        "etat_administratif": etat}

        class _Inpi:
            def list_exercises(self, siren):
                return []

        class _Bodacc:
            def search_by_siren(self, siren, famille=None, limit=20):
                return {"results": [dict(e) for e in events],
                        "total_count": len(events)}

        class _Noop:
            pass

        monkeypatch.setattr("oto_mcp.fod.fr.entreprises", _E())
        monkeypatch.setattr("oto_mcp.fod.fr.inpi", _Inpi())
        monkeypatch.setattr("oto_mcp.fod.fr.bodacc", _Bodacc())
        monkeypatch.setattr("oto_mcp.fod.fr.egapro", _Noop())
        from oto_mcp.tools import fr
        reg = _Reg()
        fr.register(reg)
        return reg.tools["fr_get"](siren="123456789")
    return _make


def test_actif_au_registre_mais_liquidation_cloturee(fr_get):
    res = fr_get("A", [_CLOTURE_LJ, _OUVERTURE_LJ])
    assert res["identity"]["etat_administratif"] == "A", "la donnée source reste rendue telle quelle"
    assert res["procedure_collective_cloturee"] == {
        "nature": "Jugement de clôture pour insuffisance d'actif",
        "date_jugement": "2019-05-14", "date_parution": "2019-05-14",
        "id": "A201900010001"}
    assert "closed" in res["etat_administratif_avertissement"]


def test_cessee_au_registre_pas_d_avertissement(fr_get):
    res = fr_get("C", [_CLOTURE_LJ])
    assert res["procedure_collective_cloturee"]["id"] == "A201900010001"
    assert "etat_administratif_avertissement" not in res


def test_la_cloture_d_une_sauvegarde_ne_dit_pas_morte(fr_get):
    res = fr_get("A", [_CLOTURE_SAUVEGARDE])
    assert "procedure_collective_cloturee" not in res
    assert "etat_administratif_avertissement" not in res


def test_une_liquidation_ouverte_seule_n_est_pas_cloturee(fr_get):
    res = fr_get("A", [_OUVERTURE_LJ])
    assert "procedure_collective_cloturee" not in res
