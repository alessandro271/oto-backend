"""A team key, called without `_group` (oto-backend#1317).

A member of a team that holds a connector key calls a tool without naming the team
(`_group=` absent, no active team): the cascade reads only the call's or the active team,
so nothing resolves. The refusal used to say a bare "no key" first, and its team hint
named a call token that does not exist (`group=`, the axis is `_group=`).

What this file holds, on a real base and through the real resolver:
1. the team that holds the key OPENS the refusal, named, with its `_group=<id>`;
2. several teams: all are listed, none is picked;
3. a team the caller is not a member of is neither served nor named;
4. the personal key, the active team and an explicit `_group=` still win, unchanged;
5. the refusal costs as many SQL queries with one team as with five (no read per team).

Ids, names and keys are fictitious.
"""
from __future__ import annotations

import itertools
import os

import pytest

from oto_mcp.mcp_errors import McpError

CONNECTEUR = "sivan"
_n = itertools.count()


@pytest.fixture(scope="module")
def base(live):
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
    try:
        yield
    finally:
        if avant is None:
            os.environ.pop("OTO_MCP_MASTER_KEY", None)
        else:
            os.environ["OTO_MCP_MASTER_KEY"] = avant


def _monde(equipes: int, *, membre_de=None, cles=()):
    """An org, a member, `equipes` teams (member of those in `membre_de`, all by default),
    a `CONNECTEUR` key on the teams of `cles` (indexes). Returns (sub, org, [gid])."""
    from oto_mcp import credentials_store, db, group_store, org_store
    i = next(_n)
    sub = f"membre-1317-{i}"
    db.upsert_user(sub, email=f"{sub}@example.invalid")
    org = org_store.create_org(f"org-1317-{i}", created_by=sub)
    org_store.add_org_member(org, sub, "org_member")
    org_store.set_active_org(sub, org)
    gids = [group_store.create_group(org, f"equipe-{i}-{k}", created_by=sub)
            for k in range(equipes)]
    for k, g in enumerate(gids):
        if membre_de is None or k in membre_de:
            group_store.add_group_member(g, sub)
    for k in cles:
        credentials_store.set_credential("group", str(gids[k]), CONNECTEUR,
                                         f"cle-equipe-{k}", set_by=sub)
    return sub, org, gids


def _resoudre(sub):
    from oto_mcp import access
    return access._resolve_credential_impl(CONNECTEUR, "auto", sub)


def _refus(sub) -> str:
    with pytest.raises(McpError) as e:
        _resoudre(sub)
    return str(e.value)


def test_une_equipe_porte_la_cle_le_refus_la_nomme_d_abord(base):
    sub, _, gids = _monde(2, cles=(0,))
    msg = _refus(sub)
    assert msg.startswith(f"No `{CONNECTEUR}` key resolves for this call"), msg
    assert f"`_group={gids[0]}`" in msg and "equipe-" in msg
    assert f"_group={gids[1]}" not in msg          # the team without a key is not offered
    assert "group=" not in msg.replace("_group=", "")   # no call token that does not exist


def test_plusieurs_equipes_l_ambiguite_est_nommee(base):
    sub, _, gids = _monde(3, cles=(0, 2))
    msg = _refus(sub)
    assert f"`_group={gids[0]}`" in msg and f"`_group={gids[2]}`" in msg
    assert "None is picked" in msg
    assert f"_group={gids[1]}" not in msg


def test_l_equipe_d_un_autre_n_est_ni_servie_ni_nommee(base):
    sub, _, gids = _monde(2, membre_de={1}, cles=(0,))
    msg = _refus(sub)
    assert msg.startswith(f"No `{CONNECTEUR}` key configured for you."), msg
    assert f"_group={gids[0]}" not in msg and "equipe-" not in msg


def test_la_cle_perso_passe_devant_l_equipe(base):
    from oto_mcp import credentials_store
    sub, org, _ = _monde(1, cles=(0,))
    credentials_store.set_credential(credentials_store.MEMBER,
                                     credentials_store.member_id(org, sub), CONNECTEUR,
                                     "cle-perso", set_by=sub)
    rc = _resoudre(sub)
    assert (rc.mode, rc.secret) == ("user", "cle-perso")


def test_l_equipe_active_passe_devant(base):
    from oto_mcp import group_store
    sub, _, gids = _monde(2, cles=(0, 1))
    group_store.set_active_group(sub, gids[1])
    rc = _resoudre(sub)
    assert (rc.mode, rc.entity_id, rc.secret) == ("group", str(gids[1]), "cle-equipe-1")


def test_group_explicite_inchange(base):
    from oto_mcp import session_org
    sub, _, gids = _monde(2, cles=(0, 1))
    jeton = session_org.set_call_group(gids[0])
    try:
        rc = _resoudre(sub)
    finally:
        session_org.reset_call_group(jeton)
    assert (rc.mode, rc.entity_id, rc.secret) == ("group", str(gids[0]), "cle-equipe-0")


def _requetes_du_refus(sub) -> int:
    import psycopg
    from unittest import mock
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        _refus(sub)
    return len(vues)


# Measured on 23/10/2026 (L7 shadow off): 29 queries with a team key, 30 without, from 1 to 5
# teams. Before #1317, each team was read: 30 → 33 with a key, 30 → 38 without. A margin.
_BORNE_REFUS = 36


def test_le_refus_ne_lit_pas_une_fois_par_equipe(base, monkeypatch):
    """The refusal asks of EVERY team of the caller whether it holds the key: in two
    queries (`group_store.groups_holding`), never one per team. Measured without the L7
    shadow, whose chain walks every team by design and is switched off by its own flag."""
    monkeypatch.setenv("OTO_L7_SHADOW", "0")
    portent = [_monde(n, cles=range(n))[0] for n in (1, 5)]
    sans = [_monde(n)[0] for n in (1, 5)]
    une, cinq = (_requetes_du_refus(s) for s in portent)
    une_sans, cinq_sans = (_requetes_du_refus(s) for s in sans)
    # Without a team key, the loans are read too (one query more): constant all the same.
    assert (une, une_sans) == (cinq, cinq_sans), (une, cinq, une_sans, cinq_sans)
    assert max(une, une_sans) <= _BORNE_REFUS, (une, une_sans)
