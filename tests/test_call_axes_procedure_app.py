"""`oto_procedure_app` reçoit `_org=` et `_group=`, comme `oto_procedure op=get`.

Écrit à la main (`@mcp.tool` brut), il n'est ni une capacité (qui reçoit `_org=` par
`_mcp_adapter`) ni un tool de travail : sans entrée explicite, aucun axe d'org ne lui
était advertisé, et un compte multi-org ne pouvait montrer que les procédures de son
org maison — alors qu'`oto_procedure op=get` venait de les lire sous `_org=`.
"""
from oto_mcp import call_axes


def _params(name: str) -> set[str]:
    return {a.param for a in call_axes.axes_for_call(name)}


def test_procedure_app_takes_org_and_group():
    params = _params("oto_procedure_app")
    assert "_org" in params
    assert "_group" in params


def test_procedure_app_advertises_org_in_listing():
    params = {a.param for a in call_axes.axes_for_listing("oto_procedure_app", {})}
    assert "_org" in params


def test_other_meta_tools_unchanged():
    # L'entrée est nominative : elle n'ouvre pas l'axe à tout le spine `oto_*`.
    assert "_org" not in _params("oto_create_org")
