"""Une pose d'index d'unicité interrompue nettoie TOUJOURS derrière elle.

Constaté en production le 07/09/2026 à 23:40:06 UTC : un `DeadlockDetected` sur la
route de pose de schéma. Le bloc de rattrapage listait deux causes — `LockNotAvailable`
et `QueryCanceled` — et l'interblocage n'en est pas une sous-classe : **les trois sont
sœurs sous `OperationalError`, aucune n'attrape les autres.**

Deux conséquences, et la seconde est la grave :

1. l'appelant recevait une erreur interne au lieu d'un refus nommé ;
2. **le nettoyage ne tournait pas.** Or un `CREATE INDEX CONCURRENTLY` coupé laisse un
   index INVALIDE, et un unique invalide peut continuer d'imposer sa contrainte aux
   écritures suivantes — le code le dit lui-même : « le laisser ferait refuser des
   écritures au nom d'un index que personne ne sait nommer ».

⚠️ **La leçon est dans la FORME, pas dans la cause manquante.** L'invariant est « toute
interruption laisse un index à retirer ». Une liste d'exceptions ne peut pas exprimer un
invariant : elle ne peut qu'énumérer ce qu'on a déjà rencontré. Ce banc éprouve donc le
nettoyage sur une exception qui n'est dans AUCUNE liste — parce que c'est la prochaine
qui manquera.
"""
from __future__ import annotations

import psycopg
import pytest

from oto_mcp.db import datastore as dsdb
from oto_mcp.db.datastore import _POSE_INTERROMPUE


class _ConnFactice:
    """Une connexion qui casse sur la création d'index et note ce qu'on lui demande."""

    def __init__(self, boum: Exception):
        self.boum = boum
        self.vues: list[str] = []

    def execute(self, requete, *a, **k):
        texte = requete.as_string(None) if hasattr(requete, "as_string") else str(requete)
        self.vues.append(texte)
        if "CREATE UNIQUE INDEX" in texte:
            raise self.boum
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _poser(monkeypatch, boum: Exception):
    conn = _ConnFactice(boum)
    monkeypatch.setattr(dsdb, "_connect_autocommit", lambda **k: conn)
    with pytest.raises(Exception) as e:
        dsdb.datastore_ensure_key_index(1, "siren")
    return conn, e.value


def _a_nettoye(conn) -> bool:
    return any("DROP INDEX IF EXISTS" in v and "_v2" in v for v in conn.vues[1:])


# ── La cause qui manquait ────────────────────────────────────────────────────

def test_un_interblocage_est_un_refus_NOMMÉ_pas_une_erreur_interne(monkeypatch):
    """Le cas de production. Un interblocage est une contention, pas une panne : il
    mérite le même refus actionnable que ses deux sœurs."""
    conn, err = _poser(monkeypatch, psycopg.errors.DeadlockDetected("boum"))

    assert type(err).__name__ == "KeyIndexUnavailable"
    assert "deux poses de schéma se sont croisées" in str(err), (
        "la cause doit être dite : elle change ce que l'appelant doit en penser")
    assert "Le schéma EST écrit" in str(err), "et ce qui a MARCHÉ doit être dit aussi"


def test_un_interblocage_NETTOIE_l_index_invalide(monkeypatch):
    """⚠️ La conséquence grave de l'omission, et la vraie raison de ce lot."""
    conn, _ = _poser(monkeypatch, psycopg.errors.DeadlockDetected("boum"))

    assert _a_nettoye(conn), (
        "un index invalide laissé derrière refuse des écritures au nom d'un index "
        "que personne ne sait nommer")


# ── ⚠️ L'épreuve qui compte : une cause qu'AUCUNE liste ne prévoit ───────────

def test_le_nettoyage_tourne_sur_UNE_CAUSE_INCONNUE(monkeypatch):
    """**Le banc qui garde la forme, pas le cas.**

    Sans lui, la correction d'aujourd'hui serait « ajouter l'interblocage à la
    liste » — et la prochaine cause manquerait exactement pareil. On éprouve donc
    avec une exception qui n'est dans aucune famille : le nettoyage doit tourir quand
    même, parce que l'index invalide, lui, ne se soucie pas de la classe de l'erreur."""
    inconnue = psycopg.errors.SerializationFailure("cause imprévue")
    assert not isinstance(inconnue, _POSE_INTERROMPUE), (
        "ce banc n'a de sens que si l'exception choisie est HORS des familles connues")

    conn, err = _poser(monkeypatch, inconnue)

    assert _a_nettoye(conn), "le nettoyage ne doit dépendre d'aucune liste"
    assert err is inconnue, (
        "une cause hors contention remonte TELLE QUELLE — la traduire en « réessaie » "
        "ferait attendre un tir suivant qui échouera pareil")


@pytest.mark.parametrize("boum", [
    psycopg.errors.LockNotAvailable("borne"),
    psycopg.errors.QueryCanceled("borne"),
])
def test_les_deux_causes_historiques_sont_INCHANGÉES(boum, monkeypatch):
    """La contre-épreuve : élargir le rattrapage ne doit rien changer à ce qui
    marchait — même refus nommé, même message sur la transaction qui retenait."""
    conn, err = _poser(monkeypatch, boum)

    assert type(err).__name__ == "KeyIndexUnavailable"
    assert "une transaction ouverte le retenait" in str(err)
    assert _a_nettoye(conn)
