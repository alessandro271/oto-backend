"""#1351 (2a) — `fr_get` rend le libellé NAF à côté du code.

L'amont sert `activite_principale` en code seul (« 58.11Z ») : une qualification qui
doit citer « le code et les mots qui vont avec » motivait un ciblage sur le seul
code. Le libellé vient de la nomenclature NAF rév. 2 de la lib de connecteurs.

Banc : le VRAI `fr_get` (proxies FOD stubés avant l'enregistrement).
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


_IDENTITE = {
    "siren": "123456789", "nom_complet": "EDITIONS EXEMPLE",
    "etat_administratif": "A", "activite_principale": "58.11Z",
    "section_activite_principale": "J",
    "siege": {"siret": "12345678900011", "activite_principale": "58.11Z",
              "est_siege": True},
    "matching_etablissements": [
        {"siret": "12345678900029", "activite_principale": "47.61Z"},
        # Code de l'ancienne nomenclature (établissement fermé) : pas de libellé
        # plutôt qu'un faux.
        {"siret": "12345678900037", "activite_principale": "221A"},
    ],
    "dirigeants": [],
}


@pytest.fixture()
def fr_get(monkeypatch):
    class _E:
        def get_by_siren(self, siren):
            return dict(_IDENTITE)

    class _Inpi:
        def list_exercises(self, siren):
            return []

    class _Bodacc:
        def search_by_siren(self, siren, famille=None, limit=20):
            return {"results": [], "total_count": 0}

    class _Noop:
        pass

    monkeypatch.setattr("oto_mcp.fod.fr.entreprises", _E())
    monkeypatch.setattr("oto_mcp.fod.fr.inpi", _Inpi())
    monkeypatch.setattr("oto_mcp.fod.fr.bodacc", _Bodacc())
    monkeypatch.setattr("oto_mcp.fod.fr.egapro", _Noop())
    from oto_mcp.tools import fr
    reg = _Reg()
    fr.register(reg)
    return reg.tools["fr_get"]


def test_le_libelle_accompagne_le_code(fr_get):
    ident = fr_get(siren="123456789")["identity"]
    assert ident["activite_principale"] == "58.11Z"
    assert ident["activite_principale_libelle"] == "Édition de livres"
    assert ident["siege"]["activite_principale_libelle"] == "Édition de livres"
    assert ident["etablissements"][0]["activite_principale_libelle"] == \
        "Commerce de détail de livres en magasin spécialisé"


def test_un_code_hors_nomenclature_n_a_pas_de_libelle(fr_get):
    etab = fr_get(siren="123456789")["identity"]["etablissements"][1]
    assert etab["activite_principale"] == "221A"
    assert "activite_principale_libelle" not in etab


def test_une_nomenclature_absente_leve(monkeypatch):
    from oto_mcp.tools import fr
    monkeypatch.setattr(fr, "_NAF_LABELS", {})
    monkeypatch.setattr("oto.tools.sirene.load_naf_codes", lambda: {})
    with pytest.raises(RuntimeError, match="NAF nomenclature missing"):
        fr._naf_labels()
