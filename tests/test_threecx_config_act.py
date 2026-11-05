"""`threecx_config` (lit la configuration) et `threecx_act` (la change).

Ce que ce fichier verrouille :
- les annotations : le lecteur est `LECTURE`, l'acteur `ENVOI` (le client MCP fait
  confirmer celui qui change le standard) ;
- l'aperçu PAR DÉFAUT : sans `dry_run=False`, aucune écriture n'est appelée sur le
  client ; l'aperçu d'une modification donne l'avant / après par propriété ;
- l'écriture RELUE : après `update`, ce que le standard tient est relu et comparé
  (`verified`, `not_held`) — un accusé n'est pas une écriture ;
- les secrets masqués dans tout ce qui est rendu ;
- le partage des rôles : une fonction se lit par le lecteur, une action passe par
  l'acteur, chacun refuse l'autre en le nommant ;
- un argument fourni qu'une op n'utilise pas est refusé.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto.tools.threecx import Schema
from oto_mcp.mcp_errors import McpError

_META = b"""<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
<Schema Namespace="Pbx" xmlns="http://docs.oasis-open.org/odata/ns/edm">
 <EntityType Name="InboundRule"><Key><PropertyRef Name="Id"/></Key>
  <Property Name="Id" Type="Edm.Int32"/><Property Name="RuleName" Type="Edm.String"/>
  <Property Name="OfficeHoursDestination" Type="Pbx.Destination"/></EntityType>
 <EntityType Name="User"><Key><PropertyRef Name="Id"/></Key>
  <Property Name="Id" Type="Edm.Int32"/><Property Name="FirstName" Type="Edm.String"/>
  <Property Name="AuthPassword" Type="Edm.String"/></EntityType>
 <EntityType Name="VoicemailSettings"><Key><PropertyRef Name="Id"/></Key>
  <Property Name="Id" Type="Edm.Int32"/><Property Name="Quota" Type="Edm.Int32"/></EntityType>
 <ComplexType Name="Destination"><Property Name="To" Type="Edm.String"/>
  <Property Name="Number" Type="Edm.String"/><Property Name="Name" Type="Edm.String"/></ComplexType>
 <Action Name="SendWelcomeEmail" IsBound="true"><Parameter Name="bindingParameter" Type="Pbx.User"/></Action>
 <Function Name="GetFirstAvailable" IsBound="true"><Parameter Name="bindingParameter" Type="Collection(Pbx.User)"/><ReturnType Type="Edm.String"/></Function>
 <Function Name="GetPhoneSecret" IsBound="true"><Parameter Name="bindingParameter" Type="Pbx.User"/><ReturnType Type="Edm.String"/></Function>
 <EntityContainer Name="Default">
  <EntitySet Name="InboundRules" EntityType="Pbx.InboundRule"/>
  <EntitySet Name="Users" EntityType="Pbx.User"/>
  <Singleton Name="VoicemailSettings" Type="Pbx.VoicemailSettings"/>
 </EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"""

_REGLE = {"Id": 7, "RuleName": "Lyon",
          "OfficeHoursDestination": {"To": "IVR", "Number": "8012", "Name": "SVI Lyon"}}
_VERS_FILE = {"OfficeHoursDestination": {"To": "Queue", "Number": "8069"}}


@pytest.fixture
def pbx(monkeypatch):
    """Le faux client : le vrai schéma, des lectures programmables, les écritures
    enregistrées."""
    c = MagicMock()
    c.schema.return_value = Schema.parse(_META)
    monkeypatch.setattr("oto_mcp.tools.threecx.client", lambda: c)
    return c


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import threecx_act, threecx_config

    m = FastMCP("t")
    threecx_config.register(m)
    threecx_act.register(m)
    return m


def _tool(name):
    return asyncio.run(_mcp().get_tool(name))


def _ecritures(c):
    return [n for n, *_ in c.method_calls if n in ("update", "create", "delete", "invoke")]


def test_annotations_lecteur_et_acteur():
    lit, agit = _tool("threecx_config").annotations, _tool("threecx_act").annotations
    assert lit.readOnlyHint is True
    assert (agit.readOnlyHint, agit.destructiveHint, agit.openWorldHint) == (False, True, True)


# --- lecture ------------------------------------------------------------------

def test_catalogue_sans_operation_secrete(pbx):
    out = _tool("threecx_config").fn(op="catalog")
    assert out["collections"] == ["InboundRules", "Users"]
    assert out["settings"] == ["VoicemailSettings"]


def test_lecture_masque_les_secrets(pbx):
    pbx.read.return_value = {"value": [{"Id": 1, "AuthPassword": "s3cret"}], "next_skip": None}
    out = _tool("threecx_config").fn(op="read", resource="Users", select=["Id", "AuthPassword"])
    assert out["value"] == [{"Id": 1, "AuthPassword": "***"}]
    kw = pbx.read.call_args.kwargs
    assert pbx.read.call_args.args == ("Users",) and kw["top"] == 50 and kw["skip"] == 0


def test_schema_d_une_ressource(pbx):
    out = _tool("threecx_config").fn(op="schema", resource="InboundRules")
    assert out["key"] == {"Id": "Edm.Int32"}
    assert set(out["properties"]["OfficeHoursDestination"]["properties"]) == {
        "To", "Number", "Name"}


def test_fonction_lue_action_renvoyee_a_l_acteur(pbx):
    pbx.invoke.return_value = {"value": "1003"}
    assert _tool("threecx_config").fn(op="function", resource="Users",
                                      operation="GetFirstAvailable") == {
        "value": {"value": "1003"}}
    with pytest.raises(McpError, match="threecx_act\\(op='call'"):
        _tool("threecx_config").fn(op="function", resource="Users", key=1,
                                   operation="SendWelcomeEmail")
    with pytest.raises(McpError, match="never called through oto"):
        _tool("threecx_config").fn(op="function", resource="Users", key=1,
                                   operation="GetPhoneSecret")


@pytest.mark.parametrize("kw", [
    {"op": "catalog", "resource": "Users"},
    {"op": "schema", "resource": "Users", "top": 5},
    {"op": "read", "resource": "Users", "operation": "X"},
    {"op": "read"},
])
def test_lecteur_refuse_les_arguments_hors_op(pbx, kw):
    with pytest.raises(McpError):
        _tool("threecx_config").fn(**kw)


# --- écriture : aperçu par défaut ---------------------------------------------

def test_modification_apercu_par_defaut_n_ecrit_rien(pbx):
    pbx.read.return_value = {"value": _REGLE}
    out = _tool("threecx_act").fn(op="update", resource="InboundRules", key=7,
                                  body={**_VERS_FILE, "RuleName": "Lyon"})
    assert out["dry_run"] is True
    assert out["changes"] == {"OfficeHoursDestination": {
        "before": _REGLE["OfficeHoursDestination"], "after": _VERS_FILE["OfficeHoursDestination"]}}
    assert out["unchanged"] == ["RuleName"]
    assert _ecritures(pbx) == []


def test_modification_ecrite_puis_relue(pbx):
    tenue = {**_REGLE, "OfficeHoursDestination": {"To": "Queue", "Number": "8069",
                                                  "Name": "File Lyon"}}
    pbx.read.return_value = {"value": tenue}
    out = _tool("threecx_act").fn(op="update", resource="InboundRules", key=7,
                                  body=_VERS_FILE, dry_run=False)
    pbx.update.assert_called_once_with("InboundRules", _VERS_FILE, key=7)
    assert out["verified"] is True and "not_held" not in out
    assert out["now"] == {"OfficeHoursDestination": tenue["OfficeHoursDestination"]}


def test_modification_non_tenue_dite(pbx):
    pbx.read.return_value = {"value": _REGLE}  # le standard n'a rien changé
    out = _tool("threecx_act").fn(op="update", resource="InboundRules", key=7,
                                  body=_VERS_FILE, dry_run=False)
    assert out["verified"] is False and out["not_held"] == ["OfficeHoursDestination"]


def test_modification_d_un_secret_refusee_meme_en_apercu(pbx):
    with pytest.raises(McpError, match="AuthPassword"):
        _tool("threecx_act").fn(op="update", resource="Users", key=1,
                                body={"AuthPassword": "x"})
    assert pbx.read.call_count == 0


def test_reglage_modifie_sans_cle(pbx):
    pbx.read.return_value = {"value": {"Id": 1, "Quota": 5}}
    out = _tool("threecx_act").fn(op="update", resource="VoicemailSettings", body={"Quota": 8})
    assert out["changes"] == {"Quota": {"before": 5, "after": 8}}


def test_suppression_apercu_puis_ecriture(pbx):
    pbx.read.return_value = {"value": _REGLE}
    out = _tool("threecx_act").fn(op="delete", resource="InboundRules", key=7)
    assert out["entity"] == _REGLE and _ecritures(pbx) == []
    out = _tool("threecx_act").fn(op="delete", resource="InboundRules", key=7, dry_run=False)
    pbx.delete.assert_called_once_with("InboundRules", key=7)
    assert out == {"written": True, "deleted": "InboundRules", "key": 7}


def test_creation_apercu_verifie_le_corps(pbx):
    out = _tool("threecx_act").fn(op="create", resource="InboundRules", body={"RuleName": "x"})
    assert out["would_create"] == "InboundRules" and _ecritures(pbx) == []
    with pytest.raises(McpError, match="no property"):
        _tool("threecx_act").fn(op="create", resource="InboundRules", body={"Nope": 1})
    pbx.create.return_value = {"Id": 9}
    assert _tool("threecx_act").fn(op="create", resource="InboundRules",
                                   body={"RuleName": "x"}, dry_run=False)["created"] == {"Id": 9}


def test_action_apercu_puis_appel_fonction_renvoyee_au_lecteur(pbx):
    out = _tool("threecx_act").fn(op="call", resource="Users", key=1,
                                  operation="SendWelcomeEmail")
    assert out["would_call"] == "SendWelcomeEmail" and _ecritures(pbx) == []
    _tool("threecx_act").fn(op="call", resource="Users", key=1,
                            operation="SendWelcomeEmail", dry_run=False)
    pbx.invoke.assert_called_once_with("SendWelcomeEmail", resource="Users", key=1,
                                       arguments=None)
    with pytest.raises(McpError, match="threecx_config\\(op='function'"):
        _tool("threecx_act").fn(op="call", resource="Users", operation="GetFirstAvailable")


@pytest.mark.parametrize("kw", [
    {"op": "update", "resource": "InboundRules", "key": 7},
    {"op": "delete", "resource": "InboundRules"},
    {"op": "delete", "resource": "InboundRules", "key": 7, "body": {"x": 1}},
    {"op": "create", "resource": "InboundRules", "body": {"RuleName": "x"}, "key": 3},
    {"op": "call", "resource": "Users"},
])
def test_acteur_refuse_les_appels_incomplets_ou_hors_op(pbx, kw):
    with pytest.raises(McpError):
        _tool("threecx_act").fn(**kw)
    assert _ecritures(pbx) == []


def test_erreur_amont_nommee(pbx):
    from oto.tools.common.errors import UpstreamHTTPError

    pbx.read.side_effect = UpstreamHTTPError(403, {"error": "forbidden"}, service="3cx")
    with pytest.raises(McpError, match="access denied \\(HTTP 403\\)"):
        _tool("threecx_config").fn(op="read", resource="Users")
