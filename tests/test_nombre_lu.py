"""`connector_verify.nombre_lu` — the one reading of a count a provider writes.

Providers move from integers to decimals without notice (AI Ark 07/10/2026,
FullEnrich 14/10/2026): each time an int-only check read a funded account as
"unreadable". One reading, the same cases everywhere."""
from __future__ import annotations

import pytest

from oto_mcp.connectors.verify import nombre_lu


@pytest.mark.parametrize("brut, attendu", [
    (42, 42), (0, 0), (-3, -3), (5404.75, 5404.75), (0.5, 0.5), (-3.5, -3.5),
    ("5404.75", 5404.75), ("5404", 5404.0), ("0", 0.0)])
def test_un_nombre_est_lu_tel_quel_sans_troncature(brut, attendu):
    assert nombre_lu(brut) == attendu


@pytest.mark.parametrize("brut", [
    None, True, False, "", "abc", "nan", "inf", "-inf", float("nan"), float("inf"),
    [5], {"balance": 5}])
def test_ce_qui_n_est_pas_un_nombre_est_illisible(brut):
    """NaN surtout : il compare faux à `<= 0` et passerait pour un compte approvisionné."""
    assert nombre_lu(brut) is None
