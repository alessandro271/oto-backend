"""`oto-mcp maintenance empreintes-2fa` : pose l'empreinte des comptes qui n'en ont pas,
lue chez Logto, sans jamais fermer de session (`auth/empreintes_2fa_pose.py`).

Base et Management API simulées : à blanc rien n'est écrit, appliquée elle pose, rejouée
elle n'a plus rien à faire ; elle pagine ; un compte non lu est compté, pas fatal ; une
empreinte posée entre-temps par le webhook n'est pas écrasée ; aucune fermeture. La
sortie : des nombres, et les codes d'`orphelins`.
"""
from __future__ import annotations

import pytest

from oto_mcp import maintenance
from oto_mcp.auth import annuaire, empreintes_2fa_pose as pose, revocation_2fa
from oto_mcp.auth import revocation_mot_de_passe as rmp

FACTEURS = {"u-a": [{"id": "totp-1", "type": "Totp"}], "u-b": [], "u-c":
            [{"id": "cle-1", "type": "WebAuthn"}]}


@pytest.fixture
def monde(monkeypatch):
    """`users` = u-a, u-b, u-c ; aucune empreinte ; Logto rend FACTEURS."""
    etat = {"comptes": ["u-a", "u-b", "u-c"], "posees": {}, "pages": [], "lus": []}

    def _page(apres, n):
        etat["pages"].append((apres, n))
        return [s for s in etat["comptes"] if s > apres and s not in etat["posees"]][:n]

    def _poser_si_absente(sub, valeur):
        if sub in etat["posees"]:
            return False
        etat["posees"][sub] = valeur
        return True

    def _facteurs(sub):
        etat["lus"].append(sub)
        if sub not in FACTEURS:
            raise annuaire.LectureInachevee(f"facteurs de {sub} : GET → 404")
        return FACTEURS[sub]

    def _interdit(*a, **k):
        raise AssertionError("la pose d'amorce ne ferme jamais rien")
    monkeypatch.setattr(pose.empreintes_2fa, "comptes_sans_empreinte", _page)
    monkeypatch.setattr(pose.empreintes_2fa, "poser_si_absente", _poser_si_absente)
    monkeypatch.setattr(pose.empreintes_2fa, "poser", _interdit)
    monkeypatch.setattr(pose.annuaire, "facteurs_2fa", _facteurs)
    monkeypatch.setattr(rmp, "fermer_tout", _interdit)
    monkeypatch.setattr(annuaire, "fermer_sessions", _interdit)
    return etat


def test_a_blanc_lit_et_compte_sans_rien_ecrire(monde):
    rapport = pose.poser_les_absentes(appliquer=False)
    assert rapport == {"applique": False, "comptes": 3, "poses": 3, "deja_posees": 0,
                       "non_lus": 0}
    assert monde["posees"] == {}
    assert pose.reste(rapport)


def test_appliquee_pose_l_empreinte_lue_puis_idempotente(monde):
    rapport = pose.poser_les_absentes(appliquer=True)
    assert rapport["poses"] == 3 and not pose.reste(rapport)
    assert monde["posees"] == {s: revocation_2fa.empreinte(f) for s, f in FACTEURS.items()}
    deuxieme = pose.poser_les_absentes(appliquer=True)
    assert deuxieme == {"applique": True, "comptes": 0, "poses": 0, "deja_posees": 0,
                        "non_lus": 0}


def test_une_empreinte_posee_par_le_webhook_n_est_pas_ecrasee(monde, monkeypatch):
    lire = pose.annuaire.facteurs_2fa

    def _webhook_entre_temps(sub):
        if sub == "u-b":
            monde["posees"]["u-b"] = "posee-par-le-webhook"
        return lire(sub)
    monkeypatch.setattr(pose.annuaire, "facteurs_2fa", _webhook_entre_temps)
    rapport = pose.poser_les_absentes(appliquer=True)
    assert rapport["poses"] == 2 and rapport["deja_posees"] == 1
    assert monde["posees"]["u-b"] == "posee-par-le-webhook"


def test_un_compte_non_lu_est_compte_et_les_autres_continuent(monde):
    monde["comptes"].insert(1, "u-absent")
    rapport = pose.poser_les_absentes(appliquer=True)
    assert rapport["non_lus"] == 1 and rapport["poses"] == 3
    assert "u-absent" not in monde["posees"]
    assert pose.reste(rapport)


def test_pagine_par_cle(monde, monkeypatch):
    monkeypatch.setattr(pose, "PAGE", 2)
    rapport = pose.poser_les_absentes(appliquer=False)
    assert rapport["comptes"] == 3
    assert monde["pages"] == [("", 2), ("u-b", 2)]


def test_commande_a_blanc_affiche_des_nombres_et_sort_3(monde, capsys):
    assert maintenance.main(["empreintes-2fa"]) == maintenance.EMPREINTES_RESTE
    sortie = capsys.readouterr().out
    assert "comptes sans empreinte : 3" in sortie
    assert "à blanc" in sortie
    assert "u-a" not in sortie, "des nombres, pas des comptes"
    assert monde["posees"] == {}


def test_commande_appliquee_sort_0_puis_0(monde, capsys):
    assert maintenance.main(["empreintes-2fa", "--appliquer"]) == maintenance.EMPREINTES_FAIT
    assert "empreintes posées : 3" in capsys.readouterr().out
    assert maintenance.main(["empreintes-2fa"]) == maintenance.EMPREINTES_FAIT


def test_commande_base_en_panne_sort_2(monde, monkeypatch):
    def _tombe(apres, n):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(pose.empreintes_2fa, "comptes_sans_empreinte", _tombe)
    assert maintenance.main(["empreintes-2fa"]) == maintenance.EMPREINTES_ERREUR
