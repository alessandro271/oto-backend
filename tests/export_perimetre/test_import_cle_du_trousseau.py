"""L'import vérifie la clé que désignent les enveloppes du fichier, pas la clé d'écriture.

L'export rechiffre chaque secret sous la clé plate de la cible (`encrypt_with_key`,
`key_ref` 0x01) : une clé de données Key Manager n'existe en clair nulle part hors de son
instance. Une cible en transition (0x01 et 0x02, écriture en 0x02) doit donc importer ; une
cible sous Key Manager seul refuse, nommément. Sans base : `cle_de_l_instance` ne lit que
le manifeste et le trousseau.
"""
from __future__ import annotations

import base64

import pytest

from oto_mcp import crypto
from oto_mcp.export_perimetre import importation
from oto_mcp.export_perimetre.importation import ImportRefuse, cle_de_l_instance

PLATE = b"\x05" * 32
DEK = b"\x06" * 32


def _manifeste(cle) -> dict:
    return {"secrets": {"connector_credentials": 1}, "objets": {"liste": {}},
            "cle_cible": crypto.empreinte_cle(cle)}


def test_cible_en_transition_ecriture_02_importe_sous_sa_cle_plate(monkeypatch):
    monkeypatch.setattr(importation, "trousseau", lambda: {0x01: PLATE, 0x02: DEK})
    assert cle_de_l_instance(_manifeste(PLATE)) == PLATE
    # et l'enveloppe de l'export se relit sur la cible, par son key_ref
    env = crypto.encrypt_with_key(PLATE, "secret", "aad")
    assert base64.b64decode(env)[0] == crypto.CLE_PLATE
    monkeypatch.setattr(crypto, "trousseau", lambda: {0x01: PLATE, 0x02: DEK})
    assert crypto.decrypt(env, "aad") == "secret"


def test_un_export_rechiffre_sous_la_dek_est_refuse(monkeypatch):
    """L'empreinte de la DEK n'est pas celle de la clé 0x01 : ses enveloppes, marquées
    0x01, ne se reliraient pas."""
    monkeypatch.setattr(importation, "trousseau", lambda: {0x01: PLATE, 0x02: DEK})
    with pytest.raises(ImportRefuse, match="chiffrés sous une autre clé"):
        cle_de_l_instance(_manifeste(DEK))


def test_cible_sous_key_manager_seul_refuse_nommement(monkeypatch):
    monkeypatch.setattr(importation, "trousseau", lambda: {0x02: DEK})
    with pytest.raises(ImportRefuse, match=r"clé plate \(key_ref 0x01\).*Key Manager seule"):
        cle_de_l_instance(_manifeste(DEK))


def test_cible_sans_cle_refuse(monkeypatch):
    monkeypatch.setattr(importation, "trousseau", lambda: {})
    with pytest.raises(ImportRefuse, match="pas de clé maîtresse"):
        cle_de_l_instance(_manifeste(PLATE))
