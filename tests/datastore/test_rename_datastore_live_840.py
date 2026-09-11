"""Retour 840 (09/09/2026) — `data_rename_datastore` répondait « Erreur interne du serveur ».

Le renommage `namespace → datastore` (v1.244.0) a aliasé la colonne dans la requête de
`rename_datastore_by_id` (`namespace AS datastore`) et laissé la lecture sous l'ancien
nom : `cur["namespace"]` → `KeyError`, sur CHAQUE renommage d'un tableau existant. Trois
appels au journal le 09/09, tous en 500. Le banc de la capacité doublait cette fonction
(`monkeypatch … rename_datastore_by_id`) : rien ne l'exécutait sur une vraie base.

Contre un vrai PostgreSQL, sur le schéma réel : c'est le SQL qui portait le défaut.
"""
from __future__ import annotations

import uuid

from oto_mcp import db


def _nom() -> str:
    return "t-" + uuid.uuid4().hex[:8]


def test_renommer_un_tableau_existant_ne_tombe_plus(live):
    ns_id = db.create_datastore("org", "840", _nom())
    nouveau = _nom()
    assert db.rename_datastore_by_id(ns_id, nouveau) is True
    assert db.get_datastore_by_id(ns_id)["datastore"] == nouveau


def test_renommer_vers_son_propre_nom_est_un_succes_sans_ecriture(live):
    """La ligne exacte qui tombait : la comparaison au nom courant."""
    nom = _nom()
    ns_id = db.create_datastore("org", "840", nom)
    assert db.rename_datastore_by_id(ns_id, nom) is True
    assert db.get_datastore_by_id(ns_id)["datastore"] == nom
