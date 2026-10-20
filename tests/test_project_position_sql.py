"""Le rang d'affichage d'un projet (`position`) n'est pas une modification du projet.

Posé SEUL, il ne touche pas `updated_at` : les listes « récemment modifiés » remonteraient
le projet à chaque glisser dans la sidebar. Posé avec un autre champ, `updated_at` bouge
pour cet autre champ, comme avant. Le SQL est lu tel qu'émis, sans base.
"""
import contextlib

import pytest

from oto_mcp.db import projects as DBP


@pytest.fixture
def emis(monkeypatch):
    requetes = []

    class _Conn:
        def execute(self, sql, params=()):
            requetes.append((sql, params))

    monkeypatch.setattr(DBP, "_connect", lambda: contextlib.nullcontext(_Conn()))
    monkeypatch.setattr("oto_mcp.db.search.stamp_rank_vector", lambda *a, **k: None)
    return requetes


def test_rang_seul_ne_touche_pas_updated_at(emis):
    DBP.update_project(7, position=1.5)
    assert emis == [("UPDATE projects SET position = %s WHERE id = %s", (1.5, 7))]


def test_rang_avec_un_autre_champ(emis):
    """`updated_at` ne prend pas de paramètre : `position` posé après lui garde l'ordre
    des valeurs aligné sur celui des colonnes."""
    DBP.update_project(7, name="X", position=1.5)
    assert emis == [("UPDATE projects SET name = %s, updated_at = NOW(), position = %s WHERE id = %s",
                     ("X", 1.5, 7))]


def test_retirer_le_rang_seul(emis):
    """`clear_position` remet le rang à NULL (non rangé), sans toucher `updated_at`."""
    DBP.update_project(7, clear_position=True)
    assert emis == [("UPDATE projects SET position = NULL WHERE id = %s", (7,))]


def test_rien_a_poser(emis):
    DBP.update_project(7)
    assert emis == []
