"""#1352 (2) — `foncier_dvf` dit pourquoi un arrondissement parisien peut dépasser le délai.

Mesuré le 26/10/2026 sur l'API publique DVF+ : la première page d'un couple
(arrondissement parisien, année) met 20 à 35 s à venir quand la source ne l'a pas
servi récemment (75115, 75116, 75117), contre ~1 s ensuite. Le délai de l'outil
(20 s, borne volontaire : l'hôte est une préproduction qui tombe) est dépassé au
premier appel. La lenteur est à la source ; la description le dit, et dit de
réessayer avant de conclure.
"""
from __future__ import annotations


class _Reg:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco(a[0]) if a and callable(a[0]) else deco


def test_la_description_dit_de_reessayer():
    from oto_mcp.tools import foncier
    reg = _Reg()
    foncier.register(reg)
    doc = reg.tools["foncier_dvf"].__doc__
    assert "Paris arrondissements" in doc and "retry once or twice" in doc
