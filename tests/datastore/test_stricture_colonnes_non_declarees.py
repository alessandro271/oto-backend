"""Datastore — REFUSER une colonne non déclarée au premier niveau (#614 / #678, oto#124).

Le cran `unknown_columns: "reject"` (opt-in, oto#127) est RETIRÉ le 05/10/2026 : toute
colonne non déclarée est refusée sur tous les tableaux (`colonnes_non_declarees`). Ce
banc tient le refus aux portes d'écriture et ce qu'il ne doit PAS casser.

⚠️ La propriété qui rend le refus tenable sur un tableau vivant : **il se juge sur ce
que le geste POSE**, jamais sur la ligne mergée — un tableau qui porte déjà des colonnes
hors schéma reste écrivable, et un patch sur une colonne sans rapport ne se fait pas
refuser pour un défaut accumulé ailleurs (même borne que `max_length`, oto-backend#284).
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg
from oto_mcp.datastore.errors import RowValidationError


FIELDS = [{"key": "siren", "type": "text"},
          {"key": "adresse", "type": "text"},
          {"key": "contacts", "type": "list",
           "of": {"fields": [{"key": "nom", "type": "text"}]}}]
SCHEMA = {"key": "siren", "fields": FIELDS}


# ── la déclaration : le cran ne se POSE plus (oto#124) ───────────────────────

def test_le_cran_ne_se_pose_plus():
    """Retiré le 05/10/2026 : le poser est refusé, avec son explication."""
    errs = dsv2.validate_schema_def({**SCHEMA, "unknown_columns": "reject"})
    assert len(errs) == 1 and "no setting any more" in errs[0], errs


# ── le geste : les cinq portes d'écriture ────────────────────────────────────

def _fake_merge_locked(rows):
    def merge_locked(ns_id, row_id, apply_fn, updated_at, **k):
        if row_id not in rows:
            return None
        merged = apply_fn(dict(rows[row_id]))
        rows[row_id] = dict(merged)
        return ({"row_id": row_id, "created_at": "t0", "updated_at": updated_at,
                 "data": dict(merged)}, merged)
    return merge_locked


@pytest.fixture()
def banc(monkeypatch):
    """Un tableau `viviers` d'UNE ligne, schéma commutable — `etat["creees"]` et
    `etat["maj"]` distinguent « rien n'a été écrit » d'une erreur rendue après coup."""
    st = DatastorePg("u", acting_org=35)
    etat = {"schema": SCHEMA,
            "lignes": {"r1": {"siren": "552081317", "adresse": "1 rue A"}},
            "creees": [], "maj": []}
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "datastore": "viviers",
                                       "schema": etat["schema"]})

    def find(ns_id, key, kv):
        for rid, data in etat["lignes"].items():
            if key and str(data.get(key)) == str(kv):
                return rid
        return None

    def insert(ns_id, rid, data, *a, **k):
        etat["creees"].append(data)
        etat["lignes"][rid] = dict(data)
        return {"row_id": rid, "created_at": "t", "updated_at": "t", "data": data}

    def get_row(ns_id, rid):
        data = etat["lignes"].get(rid)
        return ({"row_id": rid, "created_at": "t", "updated_at": "t",
                 "data": dict(data)} if data is not None else None)

    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    # Le lot juge ses clés avant d'écrire (oto#141) : le texte de chaque valeur.
    monkeypatch.setattr(dsm.db, "datastore_textes_de_cle",
                        lambda valeurs: [str(v) for v in valeurs])
    monkeypatch.setattr(dsm.db, "datastore_get_row", get_row)
    monkeypatch.setattr(dsm.db, "datastore_insert_row", insert)
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    fusion = _fake_merge_locked(etat["lignes"])

    def fusion_relevee(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : une
        # fusion ABOUTIE est une écriture, un refus levé dans `apply_fn` n'en est pas une.
        sortie = fusion(ns_id, rid, apply_fn, updated_at, **k)
        if sortie is not None:
            etat["maj"].append(rid)
        return sortie

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion_relevee)
    return st, etat


def test_creation_refusee(banc):
    store, etat = banc
    with pytest.raises(RowValidationError) as e:
        store.append_row("viviers", {"siren": "999", "_liberation": "x"})
    assert "`_liberation`" in str(e.value)
    assert etat["creees"] == []          # rien n'est parti


def test_patch_par_identifiant_refuse(banc):
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.update_row("viviers", "r1", {"_liberation": "x"})
    assert etat["maj"] == []
    assert "_liberation" not in etat["lignes"]["r1"]


def test_fusion_sur_cle_metier_refusee(banc):
    """La porte du terrain : une fiche réémise avec sa clé fusionne — et c'est là
    que les colonnes inventées entraient."""
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.append_row("viviers", {"siren": "552081317", "_liberation": "x"},
                         upsert=True)
    assert "_liberation" not in etat["lignes"]["r1"]


def test_lot_refuse(banc):
    """Le lot est le chemin le plus volumineux — c'est par lui que passent les
    imports et l'upload signé."""
    store, etat = banc
    with pytest.raises(RowValidationError):
        store.write_rows("viviers", [{"siren": "111", "_liberation": "x"}])
    assert etat["creees"] == []


# ── ce que le refus ne doit PAS casser ──────────────────────────────────────

def test_une_ecriture_dans_le_format_passe(banc):
    store, etat = banc
    store.append_row("viviers", {"siren": "552081317", "adresse": "2 rue B"},
                     upsert=True)
    assert etat["lignes"]["r1"]["adresse"] == "2 rue B"


def test_un_patch_ne_paie_pas_les_colonnes_deja_en_base(banc):
    """LE cas qui rend le refus tenable : la ligne porte DÉJÀ une colonne hors
    schéma (162 accumulées en production). Un patch sur une colonne sans rapport
    ne doit pas s'en trouver refusé — le refus juge ce que le geste POSE."""
    store, etat = banc
    etat["lignes"]["r1"]["_vieille_colonne"] = "héritée"
    store.update_row("viviers", "r1", {"adresse": "3 rue C"})
    assert etat["lignes"]["r1"]["adresse"] == "3 rue C"
    assert etat["lignes"]["r1"]["_vieille_colonne"] == "héritée"
