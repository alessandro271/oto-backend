"""Une grosse liste se sert une fiche par ligne — et reste le même JSON.

Le banc fait traverser la chaîne RÉELLE montée sur `_test_mcp()` : l'ordre compte autant
que le rendu (la rédaction a tourné avant, un champ rédigé ne revient pas).
"""
from __future__ import annotations

import asyncio
import json

from _mcp_app import static_mcp as _test_mcp

from fastmcp import Client, FastMCP
from oto.tools.common import FieldFilter

from oto_mcp import redaction
from oto_mcp.middleware import une_fiche_par_ligne

_FICHES = [{"id": i, "titre": f"Directeur marketing {i}", "ville": "Paris", "note": "x" * 120}
           for i in range(200)]


def _banc(fn, nom: str = "page"):
    m = FastMCP("banc")
    for mw in _test_mcp().middleware:
        m.add_middleware(mw)
    m.tool(name=nom)(fn)
    return m


def _servir(m: FastMCP, nom: str = "page"):
    async def appel():
        async with Client(m) as c:
            return await c.call_tool(nom, {}, raise_on_error=False)
    r = asyncio.run(appel())
    return "".join(getattr(b, "text", "") for b in r.content), r.is_error


def test_une_grosse_page_est_servie_une_fiche_par_ligne_et_reste_le_meme_json():
    charge = {"total": 200, "rows": _FICHES, "next": None}

    def page() -> dict:
        return charge

    texte, err = _servir(_banc(page))
    assert not err
    assert json.loads(texte) == charge, "le texte parsé ne change pas"
    lignes = texte.splitlines()
    assert len(lignes) == len(_FICHES) + 2, "une ligne par fiche, plus l'ouverture et la fermeture"
    assert json.loads(lignes[1].rstrip(",")) == _FICHES[0]
    assert lignes[0] == '{"total":200,"rows":['
    assert lignes[-1] == '],"next":null}'


def _compact(valeur) -> str:
    return json.dumps(valeur, ensure_ascii=False, separators=(",", ":"))


def test_une_liste_servie_seule():
    texte = une_fiche_par_ligne.rendu(_compact(_FICHES))
    assert texte is not None and json.loads(texte) == _FICHES
    assert len(texte.splitlines()) == len(_FICHES) + 2


def test_sous_le_seuil_rien_ne_change():
    petite = {"total": 2, "rows": _FICHES[:2]}
    texte, _ = _servir(_banc(lambda: petite))
    assert "\n" not in texte
    assert json.loads(texte) == petite


def test_sans_liste_de_fiches_rien_ne_change():
    assert une_fiche_par_ligne.rendu(json.dumps({"blob": "x" * 30_000})) is None
    assert une_fiche_par_ligne.rendu(json.dumps({"ids": list(range(10_000))})) is None
    assert une_fiche_par_ligne.rendu("pas du json " + "x" * 30_000) is None


def test_la_plus_lourde_des_listes_est_celle_qui_se_deplie():
    charge = {"facettes": [{"k": "a"}, {"k": "b"}], "rows": _FICHES}
    texte = une_fiche_par_ligne.rendu(_compact(charge))
    assert json.loads(texte) == charge
    assert '"facettes":[{"k":"a"},{"k":"b"}]' in texte, "la petite liste reste sur sa ligne"


def _brut(fiche: str) -> str:
    """Un document JSON brut, tel qu'un outil qui rend une `str` le sert."""
    return "[" + ",".join(fiche % i for i in range(300)) + "]"


def test_un_json_brut_qui_n_est_pas_une_forme_servie_reste_intact():
    """Parser puis réécrire n'est pas fidèle à l'octet : on ne réécrit qu'une
    sérialisation exacte de la chaîne, un autre texte reste tel quel."""
    note = "x" * 80
    for brut in (
        _brut('{"id":%d,"v":1e5,"note":"' + note + '"}'),        # 1e5 → 100000.0
        _brut('{"id":%d,"v":1.10,"note":"' + note + '"}'),       # 1.10 → 1.1
        _brut('{"id":%d,"id":0,"note":"' + note + '"}'),         # clé en double perdue
        _brut('{"id":%d,"ville":"\\u00e9","note":"' + note + '"}'),  # é échappé, compact
        json.dumps(_FICHES, indent=1),                            # indenté
    ):
        assert len(brut) >= une_fiche_par_ligne.SEUIL
        assert une_fiche_par_ligne.rendu(brut) is None


def test_la_forme_de_la_redaction_est_reprise_a_l_identique():
    """`json.dumps` par défaut (ce que réémet la rédaction) : seuls les blancs entre les
    fiches changent, les échappements restent."""
    fiches = [{**f, "ville": "Orléans"} for f in _FICHES]
    brut = json.dumps({"rows": fiches, "total": 200})
    texte = une_fiche_par_ligne.rendu(brut)
    assert texte is not None and json.loads(texte) == {"rows": fiches, "total": 200}
    assert "Orl\\u00e9ans" in texte and "é" not in texte
    assert texte.splitlines()[-1] == '], "total": 200}'
    assert texte.replace("[\n", "[").replace(",\n", ", ").replace("\n]", "]") == brut


def test_un_surrogate_echappe_traverse_la_chaine_intact():
    """`\\ud800` échappé est du JSON valide ; parsé puis réécrit, il devenait un
    surrogate seul, que la sérialisation MCP refuse (`PydanticSerializationError`)."""
    brut = _brut('{"id":%d,"s":"\\ud800","note":"' + "x" * 80 + '"}')

    def page() -> str:
        return brut

    texte, err = _servir(_banc(page))
    assert not err
    assert texte == brut


def test_une_tres_grosse_page_se_calcule_hors_de_la_boucle(monkeypatch):
    monkeypatch.setattr(une_fiche_par_ligne, "SEUIL_THREAD", 0)
    appels = []
    vrai = une_fiche_par_ligne.asyncio.to_thread

    async def espion(fn, *a):
        appels.append(fn)
        return await vrai(fn, *a)

    monkeypatch.setattr(une_fiche_par_ligne.asyncio, "to_thread", espion)
    charge = {"rows": _FICHES}
    texte, _ = _servir(_banc(lambda: charge))
    assert appels.count(une_fiche_par_ligne.rendu) == 1
    assert len(texte.splitlines()) == len(_FICHES) + 2 and json.loads(texte) == charge


def test_une_erreur_n_est_pas_touchee():
    def page() -> dict:
        raise ValueError("boum")
    _, err = _servir(_banc(page))
    assert err


def test_la_redaction_tourne_avant(monkeypatch):
    monkeypatch.setattr(redaction, "_resolve_field_filter",
                        lambda _s: FieldFilter(rules=[{"fields": ["secret"], "action": "drop"}]))
    fiches = [{**f, "secret": "NE-DOIT-PAS-SORTIR"} for f in _FICHES]

    def page() -> dict:
        return {"rows": fiches}

    texte, _ = _servir(_banc(page))
    assert "NE-DOIT-PAS-SORTIR" not in texte
    assert len(texte.splitlines()) > 100
