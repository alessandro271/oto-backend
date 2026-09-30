"""La population de `scripts/repointer_residus_alias.py` (#439) : qui est repris, qui
est ÉCARTÉ et nommé — jamais un repointage deviné."""
from __future__ import annotations

import pytest

from oto_mcp import db
from oto_mcp.db.sub_aliases import AliasNonResolvable
from scripts import repointer_residus_alias as script


class _Res:
    def __init__(self, rows=None, one=None):
        self._rows, self._one = rows or [], one

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _Conn:
    """`sub_aliases` + `users` en mémoire — seules les lectures de `population`."""

    def __init__(self, aliases, users):
        self.aliases, self.users = aliases, users   # {old: new}, {sub: suspended_at}

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT a.old_sub, EXISTS"):
            return _Res([{"old_sub": o, "vivant": o in self.users}
                         for o in sorted(self.aliases)])
        if sql.startswith("SELECT a.old_sub FROM sub_aliases a JOIN users"):
            return _Res([{"old_sub": o} for o in sorted(self.aliases) if o in self.users])
        if sql.startswith("SELECT suspended_at FROM users"):
            s = params[0]
            return _Res(one={"suspended_at": self.users[s]} if s in self.users else None)
        raise AssertionError(f"SQL imprévu : {sql}")


def test_population_reprend_les_fusionnes_et_nomme_les_ecartes(monkeypatch):
    conn = _Conn(
        aliases={"a-fusionne": "a-canon", "b-recree": "b-canon",
                 "c-casse": "c-canon", "d-pause": "d-canon"},
        users={"a-canon": None, "b-recree": None, "b-canon": None,
               "d-canon": "2026-09-01"})

    def resolve(sub):
        if sub == "c-casse":
            raise AliasNonResolvable(sub, "compte_disparu", "plus de compte")
        return conn.aliases[sub]

    monkeypatch.setattr(db, "resolve_sub", resolve)
    paires, ecartes = script.population(conn)
    assert paires == [("a-fusionne", "a-canon")]
    texte = "\n".join(ecartes)
    assert "b-recree : ancien identifiant RECRÉÉ" in texte
    assert "c-casse : chaîne non résolvable (compte_disparu)" in texte
    assert "d-pause → d-canon : compte canonique en pause" in texte


@pytest.mark.parametrize("colonne", [("tool_calls", "sub"), ("tool_calls", "effective_sub"),
                                     ("tool_calls", "view_as_sub"),
                                     ("user_account_profile", "sub")])
def test_le_constat_couvre_le_journal_et_le_profil(colonne):
    assert colonne in script._colonnes()
