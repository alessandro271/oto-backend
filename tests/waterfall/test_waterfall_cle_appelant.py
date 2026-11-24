"""Une étape « clé de l'appelant seulement » (`Step.own_key_only`) : ne part que sur une clé
PROUVÉE de l'appelant — utilisateur, équipe, org. La clé du tenant est une clé partagée du
point de vue de l'org (payée par l'opérateur, refacturée sur le tenant primaire) : elle ne
la sert pas, et l'ordre par défaut la range derrière les clés de l'appelant. Un mode
illisible (`unknown`) n'est pas une preuve."""
import pytest

from oto_mcp.waterfall import ordre, plan, telephone

from _waterfall_faux import _made, _run, calls  # noqa: F401 — la fixture des fournisseurs

APOLLO = "apollo_reveal_phone"


@pytest.mark.parametrize("mode", ["tenant", "platform", "open", "unknown"])
def test_une_cle_qui_n_est_pas_celle_de_l_appelant_saute_l_etape(calls, mode):
    calls["modes"] = {APOLLO: mode}
    out = _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane",
               providers=["apollo"])
    assert out["attempts"][0]["status"] == "skipped"
    assert out["attempts"][0]["code"] == "own_key_required"
    assert APOLLO not in _made(calls)


@pytest.mark.parametrize("mode", ["user", "group", "org"])
def test_la_cle_de_l_appelant_sert_l_etape(calls, mode):
    calls["modes"] = {APOLLO: mode}
    _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", providers=["apollo"])
    assert _made(calls)[0] == APOLLO


@pytest.mark.parametrize("mode, usable", [
    ("user", True), ("group", True), ("org", True),
    ("tenant", False), ("platform", False), ("unknown", False), ("forbidden", False)])
def test_le_plan_dit_la_meme_chose_que_l_appel(monkeypatch, mode, usable):
    monkeypatch.setattr(ordre, "_configured_steps", lambda ns: ["apollo"])
    monkeypatch.setattr(ordre, "_key_modes", lambda sub, tools: {t: mode for t in tools})
    (row,) = plan.plan("waterfall_phone", "sub-test")["steps"]
    assert row["own_key_only"] is True
    assert (row["key_mode"], row["usable"]) == (mode, usable)


def test_une_etape_ordinaire_reste_utilisable_sur_la_cle_du_tenant(monkeypatch):
    monkeypatch.setattr(ordre, "_configured_steps", lambda ns: ["kaspr"])
    monkeypatch.setattr(ordre, "_key_modes", lambda sub, tools: {t: "tenant" for t in tools})
    (row,) = plan.plan("waterfall_phone", "sub-test")["steps"]
    assert (row["key_mode"], row["usable"]) == ("tenant", True)


def test_la_cle_du_tenant_passe_derriere_celle_de_l_appelant(calls):
    """Ordre par défaut : la clé de l'appelant devant, la clé du tenant avec les autres
    clés partagées — chaque groupe dans l'ordre de la table."""
    keys = [s.key for s in telephone.PHONE_STEPS]
    first, second = keys[0], keys[1]
    tools = {s.key: s.tool for s in telephone.PHONE_STEPS}
    calls["modes"] = {tools[first]: "tenant", tools[second]: "org"}
    out = _run("waterfall_phone", linkedin_url="https://linkedin.com/in/jane", dry_run=True)
    order = [a["provider"] for a in out["attempts"]]
    assert order[0] == second and order.index(first) > 0
