"""Le rattrapage des espaces personnels ne relit plus TOUS les comptes à chaque boot
(oto-backend#534).

`backfill_personal_orgs` appelait `ensure_personal_org` pour chaque user — deux
allers-retours par compte, 1,4 s mesurés au boot de préproduction, croissant avec la
base. Il ne retient désormais que les comptes à rattraper, par une requête qui porte le
prédicat même d'`ensure_personal_org` : sans org perso VIVANTE, ou sans org ACTIVE.

Banc : un PostgreSQL réel, sur le DDL et les migrations réels de
`test_org_quota_archivage` — le correctif est un prédicat SQL, un stub ne mesurerait
que sa propre fidélité.
"""
from __future__ import annotations

import pytest

from test_org_quota_archivage import AUTRE, SUB, _espace, _perso, conn, store  # noqa: F401

TROISIEME = "sub-test-3"
QUATRIEME = "sub-test-4"


@pytest.fixture()
def rattrapes(store, monkeypatch):
    """`backfill_personal_orgs` joué, `ensure_personal_org` remplacé par un témoin : le
    banc juge QUI est retenu, pas ce qu'`ensure_personal_org` en fait (couvert ailleurs)."""
    vus: list[str] = []
    monkeypatch.setattr(store, "ensure_personal_org",
                        lambda sub, email=None, name=None: vus.append(sub))

    def _jouer() -> tuple[list[str], dict]:
        vus.clear()
        compte = store.backfill_personal_orgs()
        return sorted(vus), compte
    return _jouer


def test_un_compte_en_regle_n_est_pas_relu(store, rattrapes):
    _perso(store, SUB)                    # perso vivante, active (posée à l'adhésion)
    _perso(store, AUTRE)
    assert rattrapes() == ([], {"users": 0})


def test_sont_retenus_les_comptes_que_ensure_personal_org_reparerait(store, conn, rattrapes):
    _perso(store, SUB)                                     # en règle
    archivee = _perso(store, AUTRE)                         # perso ARCHIVÉE → à rattraper
    conn.execute("UPDATE orgs SET archived_at = now() WHERE id = %s", (archivee,))
    conn.execute("INSERT INTO users (sub) VALUES (%s), (%s)", (TROISIEME, QUATRIEME))
    # aucune org du tout → à rattraper
    _perso(store, QUATRIEME)                                # perso, mais AUCUNE active
    conn.execute("UPDATE org_members SET is_active = FALSE WHERE sub = %s", (QUATRIEME,))
    vus, compte = rattrapes()
    assert vus == sorted([AUTRE, TROISIEME, QUATRIEME])
    assert compte == {"users": 3}


def test_une_org_creee_sans_etiquette_ne_vaut_pas_espace_perso(store, rattrapes):
    """Membre actif d'une org qui n'est pas marquée `personal_of` : `ensure_personal_org`
    l'aurait réclamée — le compte reste donc retenu."""
    _perso(store, SUB)
    _espace(store, AUTRE, "Équipe")
    assert rattrapes() == ([AUTRE], {"users": 1})
