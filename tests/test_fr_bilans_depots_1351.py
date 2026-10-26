"""#1351 (1) — `fr_bilans` nomme les dépôts de comptes que le jeu INPI ne porte pas.

Le jeu des comptes annuels ne contient que les dépôts EXPLOITABLES : un dépôt
confidentiel ou non saisi n'y figure pas, et l'outil rendait un exercice vieux de
plusieurs années comme le dernier, alors que le BODACC publiait des dépôts plus
récents (trois cas mesurés : dernier exercice rendu 2016, 2017, 2019).

Banc : le VRAI `fr_bilans` (proxies FOD stubés avant l'enregistrement), avis BODACC
`dpc` à la forme servie par FOD (champ `depot` déjà décodé en objet).
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


_EXERCICES = [
    {"date_cloture_exercice": "2019-12-31", "type_bilan": "S",
     "confidentiality": "Public", "chiffre_d_affaires": 412000},
    {"date_cloture_exercice": "2018-12-31", "type_bilan": "S",
     "confidentiality": "Public", "chiffre_d_affaires": 398000},
]


def _dpc(id_, parution, cloture, descriptif=None):
    depot = {"typeDepot": "Comptes annuels et rapports", "dateCloture": cloture}
    if descriptif:
        depot["descriptif"] = descriptif
    return {"id": id_, "dateparution": parution, "familleavis": "dpc",
            "familleavis_lib": "Dépôts des comptes", "depot": depot,
            "url_complete": f"https://www.bodacc.fr/pages/annonces-commerciales-detail/?q.id=id:{id_}"}


_AVIS = [
    _dpc("C202400010001", "2024-08-01", "2023-12-31",
         "Les comptes annuels sont accompagnés d'une déclaration de confidentialité"),
    _dpc("C202300010001", "2023-07-20", "2022-12-31"),
    _dpc("C202000010001", "2020-07-10", "2019-12-31"),   # présent dans INPI
]


@pytest.fixture()
def bilans(monkeypatch):
    def _make(exercices, avis=None, panne=None):
        appels = []

        class _Inpi:
            def list_exercises(self, siren):
                return [dict(e) for e in exercices]

        class _Bodacc:
            def search_by_siren(self, siren, famille=None, limit=20):
                appels.append(famille)
                if panne:
                    raise panne
                return {"results": [dict(a) for a in avis or []],
                        "total_count": len(avis or [])}

        class _Noop:
            pass

        monkeypatch.setattr("oto_mcp.fod.fr.inpi", _Inpi())
        monkeypatch.setattr("oto_mcp.fod.fr.bodacc", _Bodacc())
        monkeypatch.setattr("oto_mcp.fod.fr.entreprises", _Noop())
        monkeypatch.setattr("oto_mcp.fod.fr.egapro", _Noop())
        from oto_mcp.tools import fr
        reg = _Reg()
        fr.register(reg)
        return reg.tools["fr_bilans"], appels
    return _make


def test_un_depot_posterieur_au_dernier_exercice_est_nomme(bilans):
    outil, appels = bilans(_EXERCICES, _AVIS)
    res = outil("123456789")
    assert appels == ["dpc"], "le croisement lit les seuls avis de dépôt des comptes"
    assert res["items"][0]["date_cloture_exercice"] == "2019-12-31"
    assert [d["date_cloture"] for d in res["depots_sans_donnees"]] == \
        ["2023-12-31", "2022-12-31"], "2019 est dans items : il n'est pas un manque"
    assert res["depots_sans_donnees"][0]["source"] == "bodacc"
    assert "confidentialité" in res["depots_sans_donnees"][0]["descriptif"]
    assert "2019-12-31" in res["avertissement"] and "2023-12-31" in res["avertissement"]


def test_rien_a_signaler_quand_les_dates_concordent(bilans):
    outil, _ = bilans(_EXERCICES, [_AVIS[2]])
    res = outil("123456789")
    assert res["depots_sans_donnees"] == []
    assert "avertissement" not in res


def test_aucun_exercice_exploitable_mais_des_depots(bilans):
    outil, _ = bilans([], _AVIS[:1])
    res = outil("123456789")
    assert res["total"] == 0
    assert res["depots_sans_donnees"][0]["date_cloture"] == "2023-12-31"
    assert "avertissement" in res


def test_une_panne_bodacc_se_dit(bilans):
    outil, _ = bilans(_EXERCICES, panne=RuntimeError("FOD: 502"))
    res = outil("123456789")
    assert res["total"] == 2
    assert "depots_sans_donnees" not in res, "un croisement absent ne se lit pas « vide »"
    assert "FOD: 502" in res["partial_errors"]["depots_sans_donnees"]
