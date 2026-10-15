"""Connecteur Nextmotion — la répartition d'UN praticien sur beaucoup de tarifs.

Ce que ce fichier verrouille :
- **seule l'entrée du praticien visé change** : les autres praticiens d'un tarif sont
  réécrits tels que lus (l'API remplace la liste entière) ;
- les deux modes : même répartition partout (liste d'ids, ou tous les tarifs d'un type de
  soin) et une répartition par tarif ; plus le retrait ;
- **`dry_run` par défaut** : l'aperçu lit chaque tarif et dit son changement, aucune
  écriture n'est appelée ; à l'écriture, seuls les tarifs qui changent sont écrits ;
- un tarif en échec n'arrête pas les autres et sort nommément (`failed`, `complete`) ;
- `data` est validé avant tout appel (UUID, champs, doublons, mélange des deux modes) ;
- l'audit d'un type de soin rend chaque tarif avec sa répartition.

Toutes les valeurs sont factices.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import nextmotion_catalogue

C = "00000000-0000-4000-8000-00000000000c"
TYPE = "00000000-0000-4000-8000-0000000000aa"
DOC = "00000000-0000-4000-8000-0000000000d1"       # le praticien qu'on ajoute
AUTRE = "00000000-0000-4000-8000-0000000000d2"     # un praticien déjà réparti
R1 = "00000000-0000-4000-8000-0000000000e1"        # répartitions comptables
R2 = "00000000-0000-4000-8000-0000000000e2"
P1 = "00000000-0000-4000-8000-0000000000f1"        # tarifs
P2 = "00000000-0000-4000-8000-0000000000f2"
P3 = "00000000-0000-4000-8000-0000000000f3"


def _tool():
    from fastmcp import FastMCP

    m = FastMCP("t")
    nextmotion_catalogue.register(m)
    return asyncio.run(m.get_tool("nextmotion_catalog")).fn


def _entry(user, dist):
    return {"user": user, "accounting_distribution": {"id": dist, "name": f"nom-{dist[-2:]}"}}


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.nextmotion.NextmotionClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("nm-test", False))
    # P1 : l'autre praticien seul ; P2 : notre praticien déjà en R2 ; P3 : déjà en R1.
    state = {P1: [_entry(AUTRE, R2)], P2: [_entry(AUTRE, R2), _entry(DOC, R2)],
             P3: [_entry(DOC, R1)]}
    inst.list_treatment_pricing_distributions.side_effect = lambda i: {"data": state[i]}
    inst.list_treatment_pricings.return_value = {
        "next": None, "data": [{"id": P1, "details": "visage", "price": "100"},
                               {"id": P2, "details": "cheveux", "price": "200"},
                               {"id": P3, "details": "corps", "price": "300"}]}
    return inst


def _written(client):
    return {c.args[0]: c.kwargs["items"]
            for c in client.set_treatment_pricing_distributions.call_args_list}


def test_meme_repartition_partout_ne_touche_que_ce_praticien(client):
    out = _tool()(kind="treatment_pricing", op="assign_distribution", dry_run=False,
                  data={"user": DOC, "accounting_distribution": R1, "items": [P1, P2, P3]})
    assert out["counts"] == {"add": 1, "update": 1, "remove": 0, "unchanged": 1}
    assert out["written"] == 2 and out["complete"] is True and out["failed"] == 0
    assert _written(client) == {
        P1: [{"user": AUTRE, "accounting_distribution": R2},
             {"user": DOC, "accounting_distribution": R1}],
        P2: [{"user": AUTRE, "accounting_distribution": R2},
             {"user": DOC, "accounting_distribution": R1}]}


def test_une_repartition_par_tarif(client):
    out = _tool()(kind="treatment_pricing", op="assign_distribution", dry_run=False,
                  data={"user": DOC, "items": [{"id": P1, "accounting_distribution": R1},
                                               {"id": P3, "accounting_distribution": R2}]})
    assert {r["id"]: r["change"] for r in out["items"]} == {P1: "add", P3: "update"}
    assert _written(client)[P3] == [{"user": DOC, "accounting_distribution": R2}]


def test_tous_les_tarifs_d_un_type_de_soin(client):
    out = _tool()(kind="treatment_pricing", op="assign_distribution", dry_run=False,
                  clinic_id=C, treatment_type_id=TYPE,
                  data={"user": DOC, "accounting_distribution": R2})
    client.list_treatment_pricings.assert_called_with(C, treatment_type_id=TYPE, limit=100,
                                                      offset=0)
    assert out["treatment_type_id"] == TYPE
    assert out["counts"] == {"add": 1, "update": 1, "remove": 0, "unchanged": 1}
    assert set(_written(client)) == {P1, P3}


def test_retrait(client):
    out = _tool()(kind="treatment_pricing", op="unassign_distribution", dry_run=False,
                  data={"user": DOC, "items": [P1, P2]})
    assert {r["id"]: r["change"] for r in out["items"]} == {P1: "unchanged", P2: "remove"}
    assert _written(client) == {P2: [{"user": AUTRE, "accounting_distribution": R2}]}


def test_dry_run_par_defaut_n_ecrit_rien_et_dit_chaque_changement(client):
    out = _tool()(kind="treatment_pricing", op="assign_distribution",
                  data={"user": DOC, "accounting_distribution": R1, "items": [P1, P2]})
    client.set_treatment_pricing_distributions.assert_not_called()
    assert out["dry_run"] is True
    p2 = next(r for r in out["items"] if r["id"] == P2)
    assert p2 == {"id": P2, "before": R2, "before_name": "nom-e2", "after": R1,
                  "change": "update"}


def test_un_tarif_en_echec_n_arrete_pas_les_autres(client):
    from oto.tools.common.errors import UpstreamHTTPError

    def put(i, items):
        if i == P1:
            raise UpstreamHTTPError(500, "boom", service="nextmotion")
        return {"data": []}

    client.set_treatment_pricing_distributions.side_effect = put
    out = _tool()(kind="treatment_pricing", op="assign_distribution", dry_run=False,
                  data={"user": DOC, "accounting_distribution": R1, "items": [P1, P2]})
    assert out["complete"] is False and out["failed"] == 1 and out["written"] == 1
    p1 = next(r for r in out["items"] if r["id"] == P1)
    assert "error" in p1 and "written" not in p1


def test_les_paquets_aussi(client):
    client.list_treatment_package_distributions.return_value = {"data": [_entry(AUTRE, R2)]}
    _tool()(kind="treatment_package", op="assign_distribution", dry_run=False,
            data={"user": DOC, "accounting_distribution": R1, "items": [P1]})
    client.set_treatment_package_distributions.assert_called_once_with(
        P1, items=[{"user": AUTRE, "accounting_distribution": R2},
                   {"user": DOC, "accounting_distribution": R1}])


@pytest.mark.parametrize("kwargs,match", [
    ({"data": {"user": DOC, "accounting_distribution": R1, "items": ["pas-un-uuid"]}},
     "UUID"),
    ({"data": {"user": DOC, "accounting_distribution": R1, "items": [P1, P1]}}, "twice"),
    ({"data": {"user": DOC, "accounting_distribution": R1,
               "items": [{"id": P1, "accounting_distribution": R2}]}}, "must be an id"),
    ({"data": {"user": DOC, "items": [P1]}}, "accounting_distribution"),
    ({"data": {"user": DOC, "accounting_distribution": R1, "items": [], "x": 1}}, "unknown"),
    ({"data": {"user": DOC, "accounting_distribution": R1, "items": [P1]},
      "treatment_type_id": TYPE, "clinic_id": C}, "not both"),
    ({"data": {"user": DOC}, "treatment_type_id": TYPE, "clinic_id": C},
     "accounting_distribution"),
    ({"data": {"user": DOC, "accounting_distribution": R1, "items": [P1]}, "item_id": P1},
     "item_id"),
])
def test_data_valide_avant_tout_appel(client, kwargs, match):
    with pytest.raises(McpError, match=match):
        _tool()(kind="treatment_pricing", op="assign_distribution", **kwargs)
    client.list_treatment_pricing_distributions.assert_not_called()
    client.set_treatment_pricing_distributions.assert_not_called()


def test_par_type_de_soin_reserve_aux_tarifs(client):
    with pytest.raises(McpError, match="treatment_pricing"):
        _tool()(kind="treatment_package", op="assign_distribution", clinic_id=C,
                treatment_type_id=TYPE, data={"user": DOC, "accounting_distribution": R1})


def test_audit_d_un_type_de_soin(client):
    out = _tool()(kind="treatment_pricing", op="distributions", clinic_id=C,
                  treatment_type_id=TYPE)
    assert out["count"] == 3
    p2 = next(p for p in out["pricings"] if p["id"] == P2)
    assert p2["details"] == "cheveux"
    assert p2["distributions"] == [
        {"user": AUTRE, "accounting_distribution": R2, "name": "nom-e2"},
        {"user": DOC, "accounting_distribution": R2, "name": "nom-e2"}]
