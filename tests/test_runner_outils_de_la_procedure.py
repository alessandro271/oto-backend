"""La liste d'outils d'un déclencheur, déduite de SA procédure — lue au bon endroit.

Mesuré le 12/09/2026 : la déduction lisait `get_guide_db` (guides à la demande),
alors que les procédures vivent dans `org_instructions` (la table de
`oto_procedure`). Pour une procédure réelle, elle rendait donc une liste VIDE.
"""
from __future__ import annotations

from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import ResolvedCtx


def test_les_outils_se_deduisent_de_la_procedure_d_org_instructions(monkeypatch):
    vu = {}
    def _lue(owner_type, owner_id, slug, version=None):
        vu.update(owner_type=owner_type, owner_id=owner_id, slug=slug)
        return {"body_md": "Réserve avec <tool:data_claim_next>, écris avec <tool:data_write>."}
    monkeypatch.setattr(RT.org_store, "get_instruction", _lue)

    outils = RT._outils_de_la_procedure(ResolvedCtx(sub="u", org_id=226), "passe-registre")

    assert vu == {"owner_type": "org", "owner_id": 226, "slug": "passe-registre"}
    assert sorted(outils) == ["data_claim_next", "data_write"]


def test_une_procedure_absente_rend_une_liste_vide(monkeypatch):
    monkeypatch.setattr(RT.org_store, "get_instruction",
                        lambda owner_type, owner_id, slug, version=None: None)
    assert RT._outils_de_la_procedure(ResolvedCtx(sub="u", org_id=226), "absente") == []
