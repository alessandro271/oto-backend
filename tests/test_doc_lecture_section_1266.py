"""Lire UNE section d'une page longue, et son plan, sans payer la page (signal #1266).

Une ingestion de rattrapage lisait des pages de 127 à 175 K caractères par `op=get` : la
réponse dépassait le plafond du client, déversée en fichier, et le run sautait une partie
de la source faute de budget. `op=outline` rend les titres ; `op=get section=` /
`region="preamble"` rend une région — adressée EXACTEMENT comme `op=patch` l'adresse.

Sur le chemin réel de la capacité (`core._doc`), sans base : seuls la lecture de la page
et les droits sont stubbés (`seams`, partagé avec les bancs du patch).

Éprouvé rouge le 2026-10-23 : `reads.get` sans la branche `_une_region` ⟹
`test_get_section_rend_la_seule_section` reçoit la page entière.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oto_mcp import doc_patch                               # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied         # noqa: E402
from oto_mcp.capabilities.docs import core as D             # noqa: E402
from oto_mcp.capabilities.docs import region                # noqa: E402
from test_doc_preamble_and_delete import CTX, seams       # noqa: E402,F401

PAGE = "\n".join([
    "> bandeau",               # 1
    "",                        # 2
    "## Équipe",               # 3
    "",                        # 4
    "### Contacts",            # 5
    "",                        # 6
    "- Alice",                 # 7
    "",                        # 8
    "### Horaires",            # 9
    "",                        # 10
    "9h-18h",                  # 11
    "",                        # 12
    "## Historique",           # 13
    "",                        # 14
    "rien",                    # 15
])


@pytest.fixture
def page(seams):
    seams["body"] = PAGE
    return seams


def _doc(**kw):
    return D._doc(CTX, D.DocInput(doc_id=662, **kw))


def test_get_section_rend_la_seule_section(page):
    out = _doc(op="get", section="contacts")
    assert out["body_md"] == "### Contacts\n\n- Alice\n"
    assert (out["heading"], out["level"], out["from_line"], out["to_line"]) == (
        "Contacts", 3, 5, 8)
    assert out["body_md_length"] == len(PAGE) and out["rev"] == "9f2c41a"
    assert page["appels"] == [], "une lecture n'écrit rien"


def test_la_section_porte_ses_sous_sections_comme_le_patch(page):
    """Même portée que `mode=delete` : le titre, puis tout jusqu'au titre de niveau ≤."""
    out = _doc(op="get", section="Équipe")
    assert out["subsections"] == ["Contacts", "Horaires"]
    attendu = doc_patch.emprise(PAGE, "Équipe", "delete")
    assert (out["from_line"], out["to_line"]) == (attendu["from_line"],
                                                  attendu["to_line"])


def test_get_region_preamble(page):
    out = _doc(op="get", region="preamble")
    assert out["body_md"] == "> bandeau\n" and out["region"] == "preamble"


def test_section_introuvable_liste_les_sections(page):
    with pytest.raises(AuthzDenied) as e:
        _doc(op="get", section="Tarifs")
    assert (e.value.status, e.value.code) == (404, "unknown_section")
    for titre in ("Équipe", "Contacts", "Horaires", "Historique"):
        assert titre in e.value.message
    assert region.POIGNEE_PREAMBULE in e.value.message


def test_le_refus_est_celui_du_patch(page):
    """Une seule résolution, une seule phrase : lire et patcher refusent pareil."""
    refus = []
    for kw in ({"op": "get"}, {"op": "patch", "body_md": "x"}):
        with pytest.raises(AuthzDenied) as e:
            _doc(section="Tarifs", **kw)
        refus.append((e.value.code, e.value.message))
    assert refus[0] == refus[1]


def test_section_ambigue_refusee(page):
    page["body"] = PAGE + "\n\n### contacts\n\nbis"
    with pytest.raises(AuthzDenied) as e:
        _doc(op="get", section="Contacts")
    assert e.value.code == "ambiguous_section"


def test_fields_refuse_sur_une_section(page):
    with pytest.raises(AuthzDenied) as e:
        _doc(op="get", section="Contacts", fields=["id"])
    assert e.value.code == "unsupported_fields"


def test_section_et_region_ensemble_refuses(page):
    with pytest.raises(AuthzDenied) as e:
        _doc(op="get", section="Contacts", region="preamble")
    assert e.value.code == "ambiguous_target"


def test_preambule_absent(page):
    page["body"] = "## Seul titre\n\ntexte"
    with pytest.raises(AuthzDenied) as e:
        _doc(op="get", region="preamble")
    assert e.value.code == "empty_preamble"


def test_get_nu_rend_toujours_la_page_entiere(page):
    assert _doc(op="get")["body_md"] == PAGE


def test_outline_rend_les_titres_sans_le_corps(page):
    out = _doc(op="outline")
    assert "body_md" not in out
    assert [(s["heading"], s["level"], s["line"], s["to_line"])
            for s in out["sections"]] == [
        ("Équipe", 2, 3, 12), ("Contacts", 3, 5, 8), ("Horaires", 3, 9, 12),
        ("Historique", 2, 13, 15)]
    assert out["preamble_length"] == len("> bandeau\n")


def test_outline_length_est_le_cout_de_la_lecture(page):
    out = _doc(op="outline")
    for s in out["sections"]:
        assert s["length"] == len(_doc(op="get", section=s["heading"])["body_md"])


def test_outline_dune_page_longue_est_courte(seams):
    """Le cas du signal : une page de ~170 K caractères, des sections de quelques K
    (la forme d'un compte rendu ou d'un portfolio), un plan de quelques K."""
    paragraphe = "Le contexte, la décision, ce qui reste ouvert et qui tranche. " * 50
    seams["body"] = "\n\n".join(f"## Réunion {n}\n\n{paragraphe}" for n in range(55))
    assert len(seams["body"]) > 170_000
    plan = _doc(op="outline")
    assert len(plan["sections"]) == 55
    assert len(str(plan)) < len(seams["body"]) / 30


def test_outline_ouvert_au_lecteur_d_un_projet_publie():
    from oto_mcp.capabilities.docs import common
    assert "outline" in common.SHARED_READ_OPS
