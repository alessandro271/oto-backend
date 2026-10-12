"""La piste « ton travail tient… » (#517) ne nomme que des tableaux LISIBLES.

`_run_id` est une poignée que n'importe qui peut taper : `claimed_hint` listait les
tableaux où le run désigné tient une ligne, sans juger si l'appelant pouvait les lire —
le run d'un autre livrait ainsi ses noms de tableaux. Le filtre est celui de
`hors_org.tenu_par_le_run` (`ownership.can_access` en lecture), jugé AVANT la lecture
du nom. Sans base : les baux, la résolution et le droit sont doublés.
"""
from __future__ import annotations

import pytest

from oto_mcp import ownership
from oto_mcp.datastore import core as ds
from oto_mcp.datastore import file_de_travail as fdt

ICI, LISIBLE, ETRANGER = 10, 11, 12
NOMS = {LISIBLE: "mes-leads", ETRANGER: "leads-d-une-autre-org"}


@pytest.fixture
def store(monkeypatch):
    s = ds.make_store("u-appelant")
    lus: list = []
    monkeypatch.setattr(fdt, "_current_run", lambda: "run-quelconque")
    monkeypatch.setattr(fdt.db, "datastore_active_leases_of", lambda run_id: [
        {"ns_id": LISIBLE, "row_id": "a"}, {"ns_id": ETRANGER, "row_id": "b"}])
    monkeypatch.setattr(type(s), "_resolve", lambda self, d, **k: ICI)
    monkeypatch.setattr(type(s), "_ns_of",
                        lambda self, n: lus.append(n) or {"datastore": NOMS[n]})
    monkeypatch.setattr(ownership, "can_access",
                        lambda sub, t, rid, lvl: rid == str(LISIBLE))
    s.lus = lus
    return s


def test_un_tableau_illisible_n_est_jamais_nomme(store):
    piste = store.claimed_hint("ma-table")
    assert "mes-leads" in piste
    assert "leads-d-une-autre-org" not in piste
    assert ETRANGER not in store.lus, "le nom d'un tableau illisible n'est même pas lu"


def test_sans_tableau_lisible_ailleurs_rien_n_est_dit(store, monkeypatch):
    monkeypatch.setattr(ownership, "can_access", lambda *a: False)
    assert store.claimed_hint("ma-table") is None
