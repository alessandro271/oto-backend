"""Une ligne ENVELOPPÉE fabriquait une colonne parasite, en silence (#117).

Sur les routes d'écriture, **le corps EST la ligne**. Qui suit la convention habituelle
l'enveloppe dans un objet qui la nomme — `{"row": {…}}` — et crée une colonne réellement
appelée `row`, contenant toute la ligne.

⚠️ **Aucune garde ne mordait, et la réponse était indiscernable d'une écriture réussie.**
Le piège a produit deux verdicts faux au cours d'une seule mesure : l'agent croit avoir
écrit, et rien ne le contredit.

**Le critère est l'ABSENCE TOTALE de correspondance, pas la présence d'une clé inconnue.**
Une clé inconnue est refusée par sa propre garde (`colonnes_non_declarees`, oto#124) ;
celle-ci nomme l'accident à la création : une ligne qui ne touche **pas une seule** des
colonnes déclarées, refusée en disant que le corps EST la ligne.

⚠️ **Mesuré avant d'être posé, sur la base servie** : 67 980 lignes de tableaux à
colonnes déclarées, **une seule** ne correspondait à rien — sur un tableau de 1 986
lignes qui déclare huit colonnes. Le critère ne décrit donc aucun régime normal ; il ne
nomme que l'accident. La garde est gratuite, comme celle des états : elle ferme la porte
avant qu'on la pousse.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore.controles import ControlesMixin
from oto_mcp.datastore.errors import ColonneNonDeclaree
from oto_mcp.datastore.hors_schema import enveloppe_probable

SCHEMA = {"fields": [{"key": "siren", "type": "text"}, {"key": "nom", "type": "text"}]}


class _Store(ControlesMixin):
    off_geles = {}; off_notices = set()
    off_erased = []; off_rejected = []; off_ignored = []; off_non_rapprochables = {}
    dernier_tableau = None

    def _trace(self, *a, **k):
        pass


def test_l_enveloppe_est_REFUSEE_a_la_creation():
    with pytest.raises(ValueError) as e:
        _Store()._check_row(SCHEMA, {"row": {"siren": "1"}}, creation=True)
    m = str(e.value)
    assert "nothing was written" in m, "l'appelant doit savoir où il en est"
    assert "The body IS the row" in m, "la cause, en une phrase"
    assert "`siren`" in m, "et les colonnes réellement disponibles"
    assert "data_set_schema" in m, "la sortie si ces clés sont vraiment ses données"


def test_un_PATCH_n_est_PAS_touche():
    """⚠️ La borne qui rend la garde juste. Un patch vise une ligne EXISTANTE par son
    `id` et peut légitimement ne toucher qu'une colonne libre — l'y refuser casserait
    un geste courant pour attraper un accident qui n'arrive qu'à la création. La
    colonne `row`, non déclarée, y est refusée par SA garde — pas comme enveloppe."""
    with pytest.raises(ColonneNonDeclaree) as e:
        _Store()._check_row(SCHEMA, {"row": {"siren": "1"}})
    assert "`row`" in str(e.value) and "The body IS the row" not in str(e.value)


def test_une_colonne_LIBRE_n_est_pas_une_enveloppe():
    """La garde ne vise que l'absence TOTALE de correspondance : une ligne qui touche
    une colonne déclarée n'est pas une enveloppe. Sa clé non déclarée est refusée par
    la garde des colonnes, qui la nomme."""
    assert enveloppe_probable(SCHEMA, {"siren": "1", "libre": "x"}) is False
    with pytest.raises(ColonneNonDeclaree) as e:
        _Store()._check_row(SCHEMA, {"siren": "1", "libre": "x"}, creation=True)
    assert "`libre`" in str(e.value) and "The body IS the row" not in str(e.value)


def test_un_tableau_LIBRE_n_est_jamais_juge():
    """Sans colonne déclarée, il n'y a rien à quoi comparer : inventer un refus sur une
    absence d'information serait pire que le silence. La colonne non déclarée y est
    refusée par sa propre garde."""
    assert enveloppe_probable({"fields": []}, {"quoi": "x"}) is False
    with pytest.raises(ColonneNonDeclaree) as e:
        _Store()._check_row({"fields": []}, {"quoi": "x"}, creation=True)
    assert "The body IS the row" not in str(e.value)


@pytest.mark.parametrize("data", [{}, {"_id": "x"}, None, "pas un dict"])
def test_rien_a_juger_ne_refuse_rien(data):
    """Ligne vide, méta seules, données non exploitables : la primitive rend `False`
    plutôt que de lever — un contrôle de forme ne casse pas une écriture."""
    assert enveloppe_probable(SCHEMA, data) is False


def test_les_DEUX_chemins_de_creation_declarent_leur_mode():
    """⚠️ La garde contre le défaut qui revient : une garde posée sur les chemins
    auxquels on pense, absente de celui qu'on croyait couvert. `append_row` et le LOT
    créent tous deux des lignes — le fichier d'écriture porte lui-même la trace d'un
    oubli de ce genre (« les trois autres étaient branchés ; la création unitaire,
    non »)."""
    import ast
    import inspect

    from oto_mcp.datastore import ecriture, lots

    for mod in (ecriture, lots):
        trouve = False
        for n in ast.walk(ast.parse(inspect.getsource(mod))):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "_check_row"):
                kw = {k.arg: k.value for k in n.keywords}
                v = kw.get("creation")
                if isinstance(v, ast.Constant) and v.value is True:
                    trouve = True
        assert trouve, (
            f"`{mod.__name__}` ne déclare plus `creation=True` : l'enveloppe y "
            f"repasserait en silence")
