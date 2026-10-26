"""#1352 (1b) — `foncier_proprietaire` dit qu'il ne lit que le début de la liste.

Pas d'`offset` et un ordre qui est celui de la source (SIREN croissant) : sur une
grande commune ou un département, seuls les propriétaires de plus petit SIREN sont
lisibles. Ajouter un `offset` traverse trois couches (lib, service de données,
outil) ; en attendant, la description — le texte que lit l'agent — dit le manque.
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


def test_la_description_dit_qu_il_n_y_a_pas_d_offset():
    from oto_mcp.tools import foncier
    reg = _Reg()
    foncier.register(reg)
    doc = reg.tools["foncier_proprietaire"].__doc__
    assert "No offset" in doc and "ascending owner SIREN" in doc
    assert "emprise_min" in doc
