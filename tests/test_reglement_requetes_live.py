"""Ce que coûte, en requêtes SQL, le règlement d'un appel (L8, `access/reglement.py`).

Le règlement remplace le débit que chaque outil faisait lui-même (`record_platform_usage`)
par un seul point, au bord de l'appel. Il ne coûte rien de plus : le relevé du
19/10/2026, contre une vraie base et le vrai schéma, donnait le MÊME compte avant et
après L8 pour un appel sous l'arête `user` d'un connecteur chaîné (`grants_chain`) — 7
requêtes. R6 en retire 2, la comparaison des deux compteurs : restent 5, l'historique
`usage`, l'org active, la chaîne relue (`platform_rung`) et le compteur d'arête.

Ce que ce fichier tient : le compte de chaque cas, au plus juste ; un appel qui ne
débite rien (outil sans débit, zéro unité, clé propre) ne touche pas la base ; et ce qui
est débité arrive bien dans les deux compteurs.
"""
from __future__ import annotations

import psycopg
import pytest

from oto_mcp import access

SUB = "granted-sub"

# Relevé du 19/10/2026 : 7 avant et après L8, 5 après R6. Un écart est une régression
# du chemin chaud, ou une économie à graver ici.
_CHAINE = 5
_NON_CHAINE = 1


def _compter(f) -> int:
    from unittest import mock
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        f()
    return len(vues)


def _rows(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(sql, params)


@pytest.fixture(scope="module")
def cle_chainee(live):
    """Une clé plateforme `fullenrich` (connecteur chaîné) avec un quota nominatif : la
    migration de boot en tire l'arête que le règlement débite."""
    from oto_mcp.db import init_db
    _exec("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
          "account, secret_enc, share_mode, share_down, meta) VALUES "
          "('platform', 'env', 'fullenrich', '', 'x', 'open', '[]'::jsonb, %s::jsonb)",
          (f'{{"rate_limit": 5, "rate_limit_by": {{"user:{SUB}": 200}}}}',))
    init_db()
    aretes = _rows("SELECT id FROM grants WHERE grantee_id = %s", (SUB,))
    assert len(aretes) == 1, aretes
    return aretes[0]["id"]


def _usage(provider: str) -> int:
    return sum(r["count"] for r in _rows(
        "SELECT count FROM usage WHERE sub = %s AND tool = %s", (SUB, provider)))


def _compteur(arete: int) -> int:
    return sum(r["calls"] for r in _rows(
        "SELECT calls FROM grant_counters WHERE grant_id = %s", (arete,)))


def test_un_appel_chaine_coute_ce_qu_il_coutait(cle_chainee):
    avant = (_usage("fullenrich"), _compteur(cle_chainee))
    n = _compter(lambda: access.regler_appel(
        SUB, "fullenrich_enrich_linkedin", {"platform_carrier": "fullenrich", "units": 5}))
    assert n == _CHAINE, f"{n} requêtes pour régler un appel chaîné (relevé : {_CHAINE})"
    assert (_usage("fullenrich"), _compteur(cle_chainee)) == (avant[0] + 5, avant[1] + 5)


def test_un_appel_non_chaine_ne_touche_que_l_historique(live):
    avant = _usage("cloro")
    n = _compter(lambda: access.regler_appel(
        SUB, "cloro_search", {"platform_carrier": "cloro", "units": 1}))
    assert n == _NON_CHAINE
    assert _usage("cloro") == avant + 1


@pytest.mark.parametrize("outil, releve", [
    ("tavily_search", {"platform_carrier": "tavily"}),                 # déclaré sans débit
    ("apollo_bulk_match", {"platform_carrier": "apollo", "units": 0}),  # rien de consommé
    ("serper_search", {"units": 3, "quantity": 3}),                     # clé propre
])
def test_un_appel_qui_ne_debite_rien_ne_touche_pas_la_base(live, outil, releve):
    assert _compter(lambda: access.regler_appel(SUB, outil, releve)) == 0


# ── le budget d'unités sur le chemin d'un appel (#1148) ──────────────────────
# Relevé du 19/10/2026 : la réservation, que la résolution de la clé plateforme fait
# pour un outil du périmètre (`resolve.py` → `credits.reserver`), coûte 2 requêtes (la
# réservation, puis le solde), barème et modes de payeur en mémoire ; hors du
# périmètre, aucune.
_RESERVATION = 2


def test_la_reservation_coute_deux_ecritures_sur_le_chemin_d_un_appel(live, monkeypatch):
    import uuid

    from oto_mcp import geste, session_org
    from oto_mcp.access import credits as budget
    from oto_mcp.db import credits as db_credits
    from oto_mcp.tools import fullenrich

    monkeypatch.setenv("OTO_CREDITS_MODE", "report")
    monkeypatch.setattr(budget, "_PERIMETRE", {})
    budget.declarer("fullenrich_enrich_linkedin", "fullenrich", "submit",
                    unites=fullenrich._pire_par_nature)
    db_credits.poser_bareme(1, [{"connector": "fullenrich", "function": "submit",
                                 "unit": u, "budget": b, "per_n": 1}
                                for u, b in (("work_emails", 5), ("personal_emails", 20),
                                             ("phones", 60))])
    budget.oublier()
    contacts = {"contacts": [{"first_name": "a", "last_name": "b", "domain": "x.fr"}]}

    def _reserver(outil: str) -> dict:
        releve: dict = {}
        jeton = session_org.set_call_trace(releve)
        try:
            # Le geste de l'appel, comme le pose le journal d'appels : la clé de la
            # réservation (`tool_calls.call_uid`), sans requête.
            with geste.portee(geste.AGENT, SUB, uuid.uuid4().hex):
                budget.ouvrir_appel(outil, contacts)
                budget.reserver(SUB, None, "platform")
        finally:
            session_org.reset_call_trace(jeton)
        return releve

    _reserver("fullenrich_enrich_linkedin")          # barème et modes mis en mémoire
    assert _compter(lambda: _reserver("serper_search")) == 0
    assert _compter(lambda: _reserver("fullenrich_enrich_linkedin")) == _RESERVATION
    releve = _reserver("fullenrich_enrich_linkedin")
    assert releve[budget.CLE]["hold"]["reserved"] == 65, \
        "1 contact, par nature : e-mail pro (5) + téléphone (60)"
    budget.oublier()
