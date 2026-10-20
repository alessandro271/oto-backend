"""Un seul fil compte une campagne à l'échéance du cache (#1108).

Les sondages de l'ordonnanceur arrivent par les fils du pool. Avant le verrou, chaque
fil qui trouvait le compte d'une campagne périmé le recalculait : 8 à 14 fils sur la
même chaîne (`tableau_vise` → appartenance → lecture du tableau), et ce convoi tenait
le GIL assez longtemps pour geler la boucle (1 à 2,2 s, 83 % des gels d'une semaine).
"""
from __future__ import annotations

import threading
import time

import pytest

from oto_mcp.capabilities import _lignes_reservables as LR

N_FILS = 12


@pytest.fixture(autouse=True)
def _cache_vierge():
    LR._CACHE.clear()
    LR._SIGNALE.clear()
    LR._VERROUS.clear()
    yield
    LR._CACHE.clear()
    LR._SIGNALE.clear()
    LR._VERROUS.clear()


def _campagne(fid: int, ns: int = 70) -> dict:
    return {"id": fid, "namespace": str(ns), "sub": "u1", "org_id": 35, "row_filter": None}


def _brancher(monkeypatch, *, echoue: bool = False, duree: float = 0.2):
    """Le calcul d'un compte, instrumenté : combien de fois il a lieu, et lent assez
    pour que tous les fils ratent le cache pendant qu'il tourne."""
    calculs: list[int] = []

    def tableau_vise(f):
        calculs.append(int(f["id"]))
        time.sleep(duree)
        if echoue:
            raise LookupError("tableau introuvable")
        return {"id": int(f["namespace"]), "schema": None}

    monkeypatch.setattr(LR, "tableau_vise", tableau_vise)
    monkeypatch.setattr(LR, "perimetre_de_reservation",
                        lambda schema, ns_id, filtre: (None, ["clause"]))
    monkeypatch.setattr(LR.db, "datastore_compter_reservables",
                        lambda ns_id, perimetres: {fid: 42 for fid in perimetres})
    return calculs


def _en_meme_temps(appels) -> list:
    """Lance chaque appel dans son fil, tous relâchés ensemble, et rend leurs résultats."""
    depart = threading.Barrier(len(appels))
    resultats: list = [None] * len(appels)
    erreurs: list = []

    def fil(i, appel):
        depart.wait()
        try:
            resultats[i] = appel()
        except BaseException as e:  # noqa: BLE001 — remonté par l'assertion ci-dessous
            erreurs.append(e)

    fils = [threading.Thread(target=fil, args=(i, a)) for i, a in enumerate(appels)]
    for t in fils:
        t.start()
    for t in fils:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in fils), "interblocage : un fil attend toujours"
    assert not erreurs, erreurs
    return resultats


def test_n_fils_ratent_le_cache_ensemble_un_seul_calcul(monkeypatch):
    calculs = _brancher(monkeypatch)
    f = _campagne(7)
    resultats = _en_meme_temps([lambda: LR.lignes_reservables([f])] * N_FILS)
    assert calculs == [7]
    assert resultats == [{7: 42}] * N_FILS


def test_un_echec_se_partage_aussi(monkeypatch):
    """Le compte qui échoue n'est pas recalculé par chaque fil : `_ECHEC` est gardé
    pour la fenêtre, et la campagne reste absente pour tous."""
    calculs = _brancher(monkeypatch, echoue=True)
    f = _campagne(7)
    resultats = _en_meme_temps([lambda: LR.lignes_reservables([f])] * N_FILS)
    assert calculs == [7]
    assert resultats == [{}] * N_FILS


def test_des_listes_croisees_ne_s_interbloquent_pas(monkeypatch):
    """Deux sondages qui partagent des campagnes, dans un ordre inverse : chacune n'est
    comptée qu'une fois, et personne n'attend l'autre en cercle."""
    calculs = _brancher(monkeypatch, duree=0.05)
    a, b, c = _campagne(1), _campagne(2, ns=71), _campagne(3)
    appels = [lambda: LR.lignes_reservables([a, b, c]),
              lambda: LR.lignes_reservables([c, b, a]),
              lambda: LR.lignes_reservables([b, c, b])] * 4
    resultats = _en_meme_temps(appels)
    assert sorted(calculs) == [1, 2, 3]
    assert all(set(r) <= {1, 2, 3} and set(r.values()) == {42} for r in resultats)
    assert resultats[0] == {1: 42, 2: 42, 3: 42}


def test_le_ttl_et_la_signature_gardent_leur_sens(monkeypatch):
    """Rien ne change hors concurrence : un compte vaut `TTL_S`, et une déclaration
    modifiée le recalcule tout de suite."""
    calculs = _brancher(monkeypatch, duree=0)
    f = _campagne(7)
    assert LR.lignes_reservables([f], horloge=lambda: 1000.0) == {7: 42}
    assert LR.lignes_reservables([f], horloge=lambda: 1000.0 + LR.TTL_S - 0.1) == {7: 42}
    assert calculs == [7]
    assert LR.lignes_reservables([f], horloge=lambda: 1000.0 + LR.TTL_S) == {7: 42}
    assert calculs == [7, 7]
    autre = {**f, "row_filter": {"statut": "nouveau"}}
    LR.lignes_reservables([autre], horloge=lambda: 1000.0 + LR.TTL_S)
    assert calculs == [7, 7, 7]
