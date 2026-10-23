"""PayFit — les ÉCRITURES, câblées le 21/10/2026 dans `payfit_act` (refusées depuis le
24/09/2026).

Ce que ce fichier verrouille :
- `payfit_act` est déclaré `ENVOI` (le client le fait confirmer) et n'a que des ops à
  effet ; les anciennes ops d'écriture des lecteurs rendent `op_moved`, sans clé ni
  client, en nommant l'op de `payfit_act` ;
- chaque op d'écriture appelle LA bonne méthode du client, avec les arguments
  traduits (`begin_date` → `start_date` pour une absence, `insurance_contract_ids`
  → la liste de la bonne famille) ;
- un argument requis manquant est nommé AVANT toute résolution de clé : rien ne part ;
- un argument d'une autre op est refusé, `is not None` ;
- la régularisation n'existe que pour la mutuelle ;
- le NIR et le motif d'absence passés en argument partent masqués au journal ;
- le niveau `read` ne sert aucune écriture, `full` les sert toutes ;
- `dry_run` n'est toujours le paramètre d'aucun outil.

Toutes les valeurs sont factices.
"""
import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

from oto_mcp.mcp_errors import McpError

K = "000000000000000000000a0a"
K2 = "000000000000000000000b0b"
NIR = "185057800608436"


@pytest.fixture
def client(monkeypatch):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.payfit.PayfitClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("pf-test", False))
    return inst


@pytest.fixture
def rien_ne_part(monkeypatch):
    """La clé et le client LÈVENT s'ils sont touchés."""
    def _cle(*a, **kw):
        raise AssertionError("un appel incomplet a résolu la clé PayFit")

    def _client(**kw):
        raise AssertionError("un appel incomplet a construit le client PayFit")

    monkeypatch.setattr("oto_mcp.access.resolve_api_key", _cle)
    monkeypatch.setattr("oto.tools.payfit.PayfitClient", _client)


def _mcp():
    from fastmcp import FastMCP

    from oto_mcp.tools import payfit as P
    from oto_mcp.tools import payfit_paie as PP
    from oto_mcp.tools import payfit_act as PA
    from oto_mcp.tools import payfit_social as PS

    m = FastMCP("t")
    for module in (P, PP, PS, PA):
        module.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


# --- routage ------------------------------------------------------------------------

def test_creer_un_collaborateur(client):
    client.create_collaborator.return_value = {"collaboratorId": K}
    out = _tool("payfit_act")(op="create_collaborator", first_name="Ada",
                              last_name="Test", personal_email="ada@exemple.test",
                              social_security_number=NIR, invite_collaborator=False)
    assert out == {"created": {"collaboratorId": K}}
    kw = client.create_collaborator.call_args.kwargs
    assert kw["first_name"] == "Ada" and kw["personal_email"] == "ada@exemple.test"
    assert kw["social_security_number"] == NIR and kw["invite_collaborator"] is False


def test_creer_un_contrat(client):
    client.create_contract.return_value = {"contractId": K2}
    out = _tool("payfit_act")(op="create_contract", collaborator_id=K, job_title="Poste",
                              start_date="2026-02-01")
    assert out == {"created": {"contractId": K2}}
    client.create_contract.assert_called_once_with(K, job_title="Poste",
                                                   start_date="2026-02-01")


def test_creer_un_arret_maladie(client):
    client.create_absence.return_value = {"id": K2}
    out = _tool("payfit_act")(op="create_absence", contract_id=K,
                              absence_type="fr_maladie_ordinaire",
                              begin_date="2026-02-02", end_date="2026-02-06")
    assert out == {"created": {"id": K2}}
    # Les moments omis ne sont pas transmis : le client garde ses défauts (journées
    # entières), il n'en reçoit pas d'autres.
    client.create_absence.assert_called_once_with(
        contract_id=K, absence_type="fr_maladie_ordinaire",
        start_date="2026-02-02", end_date="2026-02-06")


def test_une_demi_journee_passe_ses_moments(client):
    _tool("payfit_act")(op="create_absence", contract_id=K, absence_type="fr_rtt",
                        begin_date="2026-02-02", end_date="2026-02-02",
                        end_moment="middle-of-day")
    assert client.create_absence.call_args.kwargs["end_moment"] == "middle-of-day"
    assert "start_moment" not in client.create_absence.call_args.kwargs


def test_annuler_une_absence(client):
    out = _tool("payfit_act")(op="cancel_absence", absence_id=K, comment="saisie en double")
    assert out == {"cancelled": K}
    client.cancel_absence.assert_called_once_with(K, comment="saisie en double")


def test_affilier_a_la_mutuelle(client):
    out = _tool("payfit_act")(op="set_affiliation", contract_id=K,
                              insurance_contract_ids=[K2],
                              employee_is_exempted=False)
    assert out["affiliated"] == {"contract_id": K, "kind": "health",
                                 "insurance_contract_ids": [K2]}
    client.set_health_insurance.assert_called_once_with(
        K, health_insurance_contract_ids=[K2], employee_is_exempted=False)
    client.set_provident_fund.assert_not_called()


def test_affilier_a_la_prevoyance(client):
    _tool("payfit_act")(op="set_affiliation", kind="provident", contract_id=K,
                        insurance_contract_ids=[K2])
    client.set_provident_fund.assert_called_once_with(
        K, provident_fund_contract_ids=[K2])
    client.set_health_insurance.assert_not_called()


def test_la_prevoyance_n_a_pas_de_dispense(rien_ne_part):
    with pytest.raises(McpError, match="employee_is_exempted"):
        _tool("payfit_act")(op="set_affiliation", kind="provident", contract_id=K,
                            insurance_contract_ids=[K2],
                            employee_is_exempted=True)


def test_regulariser_la_mutuelle(client):
    client.request_health_insurance_regularization.return_value = {}
    _tool("payfit_act")(op="request_regularization", contract_id=K,
                        insurance_contract_ids=[K2], effective_date="2026-01-01")
    client.request_health_insurance_regularization.assert_called_once_with(
        K, health_insurance_contract_ids=[K2], effective_date="2026-01-01")


def test_la_prevoyance_ne_se_regularise_pas(rien_ne_part):
    with pytest.raises(McpError, match="mutuelle only"):
        _tool("payfit_act")(op="request_regularization", kind="provident", contract_id=K,
                            insurance_contract_ids=[K2],
                            effective_date="2026-01-01")


# --- arguments ----------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,manquant", [
    ({"op": "create_collaborator", "first_name": "Ada", "last_name": "Test"},
     "personal_email"),
    ({"op": "create_contract", "collaborator_id": K, "job_title": "Poste"},
     "start_date"),
    ({"op": "create_absence", "contract_id": K, "begin_date": "2026-02-02",
      "end_date": "2026-02-06"}, "absence_type"),
    ({"op": "cancel_absence"}, "absence_id"),
    ({"op": "set_affiliation", "contract_id": K}, "insurance_contract_ids"),
    ({"op": "request_regularization", "contract_id": K,
      "insurance_contract_ids": [K2]}, "effective_date"),
])
def test_un_requis_manquant_est_nomme_et_rien_ne_part(rien_ne_part, kwargs, manquant):
    with pytest.raises(McpError, match=manquant):
        _tool("payfit_act")(**kwargs)


@pytest.mark.parametrize("kwargs,intrus", [
    ({"op": "create_absence", "contract_id": K, "absence_type": "fr_rtt",
      "begin_date": "2026-02-02", "end_date": "2026-02-02", "comment": "x"}, "comment"),
    ({"op": "cancel_absence", "absence_id": K, "contract_id": K2}, "contract_id"),
    ({"op": "create_contract", "collaborator_id": K, "job_title": "Poste",
      "start_date": "2026-02-01", "absence_type": "fr_rtt"}, "absence_type"),
    ({"op": "create_collaborator", "first_name": "Ada", "last_name": "Test",
      "personal_email": "ada@exemple.test", "contract_id": K}, "contract_id"),
    ({"op": "set_affiliation", "contract_id": K, "insurance_contract_ids": [K2],
      "effective_date": "2026-01-01"}, "effective_date"),
])
def test_un_argument_d_une_autre_op_est_refuse(client, kwargs, intrus):
    with pytest.raises(McpError, match=intrus):
        _tool("payfit_act")(**kwargs)


# --- la séparation lecteur / acteur -------------------------------------------------

def test_payfit_act_est_un_envoi_et_n_a_que_des_ops_a_effet():
    import re

    tool = asyncio.run(_mcp().get_tool("payfit_act"))
    a = tool.annotations
    assert a.readOnlyHint is False and a.destructiveHint is True
    assert a.openWorldHint is True
    ops = tool.parameters["properties"]["op"]["enum"]
    assert sorted(ops) == sorted(["create_collaborator", "create_contract",
                                  "create_absence", "cancel_absence",
                                  "set_affiliation", "request_regularization"])
    assert all(re.match(r"^(create|cancel|set|request)_", op) for op in ops)
    assert "default" not in tool.parameters["properties"]["op"]


@pytest.mark.parametrize("tool,kwargs,act_op", [
    ("payfit_collaborator", {"op": "create", "first_name": "Ada",
                             "social_security_number": NIR}, "create_collaborator"),
    ("payfit_contract", {"op": "create", "collaborator_id": K}, "create_contract"),
    ("payfit_absence", {"op": "create", "absence_type": "fr_maladie_ordinaire"},
     "create_absence"),
    ("payfit_absence", {"op": "cancel", "absence_id": K}, "cancel_absence"),
    ("payfit_insurance", {"op": "affiliate", "contract_id": K}, "set_affiliation"),
    ("payfit_insurance", {"op": "regularize", "contract_id": K},
     "request_regularization"),
])
def test_une_ancienne_op_d_ecriture_d_un_lecteur_renvoie_vers_payfit_act(
        rien_ne_part, tool, kwargs, act_op):
    with pytest.raises(McpError) as exc:
        _tool(tool)(**kwargs)
    err = exc.value.error
    assert err.data == {"code": "op_moved", "retryable": False, "op": kwargs["op"],
                        "tool": "payfit_act", "act_op": act_op}
    assert f'payfit_act(op="{act_op}"' in err.message
    # Le refus ne recopie rien de ce qui a été passé : ni NIR ni motif d'absence.
    assert NIR not in err.message and "fr_maladie_ordinaire" not in err.message


# --- journal et niveaux -------------------------------------------------------------

def test_nir_et_motif_partent_masques_au_journal():
    from oto_mcp.calllog import truncated_args

    col = truncated_args({"op": "create", "social_security_number": NIR},
                         tool="payfit_collaborator")
    assert NIR not in repr(col) and col["op"] == "create"
    absn = truncated_args({"op": "create", "absence_type": "fr_maladie_ordinaire"},
                          tool="payfit_absence")
    assert "fr_maladie_ordinaire" not in repr(absn)
    # L'outil qui écrit, d'abord : c'est par lui que passent les vraies valeurs.
    act = truncated_args({"op": "create_collaborator", "social_security_number": NIR,
                          "absence_type": "fr_maladie_ordinaire"}, tool="payfit_act")
    assert NIR not in repr(act) and "fr_maladie_ordinaire" not in repr(act)
    # Par `oto_call` aussi : l'outil cible est lu dans `name`.
    for cible in ("payfit_absence", "payfit_act"):
        via = truncated_args({"name": cible, "arguments": {
            "absence_type": "fr_maladie_ordinaire", "social_security_number": NIR}},
            tool="oto_call")
        assert "fr_maladie_ordinaire" not in repr(via), cible
    assert NIR not in repr(via)


_ECRITURES = [("payfit_act", op) for op in (
    "create_collaborator", "create_contract", "create_absence", "cancel_absence",
    "set_affiliation", "request_regularization")] + [
    # Les anciennes ops des lecteurs ne font que pointer vers `payfit_act` : déclarées
    # avec le scope de l'écriture, une org `read` ne voit même pas le renvoi.
    ("payfit_collaborator", "create"), ("payfit_contract", "create"),
    ("payfit_absence", "create"), ("payfit_absence", "cancel"),
    ("payfit_insurance", "affiliate"), ("payfit_insurance", "regularize")]


@pytest.mark.parametrize("tool,op", _ECRITURES)
def test_le_niveau_read_ne_sert_aucune_ecriture(tool, op):
    from oto_mcp.connectors import access_levels

    decl = access_levels.DECLARED["payfit"]
    assert not decl.covers("read", tool, op)
    assert decl.covers("full", tool, op)


def test_le_niveau_read_sert_toutes_les_lectures():
    from oto_mcp.connectors import access_levels

    decl = access_levels.DECLARED["payfit"]
    ecritures = set(_ECRITURES)
    for tool, spec in decl.tools.items():
        ops = spec if isinstance(spec, dict) else {None: spec}
        for op in ops:
            if (tool, op) not in ecritures:
                assert decl.covers("read", tool, op), (tool, op)


@pytest.mark.parametrize("tool", [
    "payfit_act", "payfit_collaborator", "payfit_contract", "payfit_absence",
    "payfit_insurance", "payfit_company", "payfit_payslip", "payfit_payroll", "payfit_worked_time",
    "payfit_meal_voucher", "payfit_document",
])
def test_dry_run_n_est_plus_un_parametre(tool):
    assert "dry_run" not in inspect.signature(_tool(tool)).parameters
