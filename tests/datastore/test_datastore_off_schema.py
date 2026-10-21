"""Datastore — un champ écrit HORS SCHÉMA est refusé (oto-backend#294, oto#124).

Le cas fondateur : un schéma renommé (`actualite_sociale` → `analyse1`), un agent
qui continue d'écrire sous les anciens noms. La valeur atterrissait dans une colonne
hors format, que l'interface et tout ce qui s'appuie sur le schéma ignorent, et
l'agent recevait un accusé de réception. Une colonne non déclarée est désormais
REFUSÉE (`ColonneNonDeclaree`), rien n'est écrit.

Par le seam `_check_row`, par lequel passent append / batch / merge / upsert / patch.
"""
import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore.core import DatastorePg, RowValidationError
from oto_mcp.datastore.errors import ColonneNonDeclaree


SCHEMA = {
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "analyse1", "type": "text"},
        {"key": "analyse2", "type": "text"},
    ],
}


# ── le store ──────────────────────────────────────────────────────────

@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    calls = {"insert": [], "update": []}
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": SCHEMA})
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data, *a, **k: (
                            calls["insert"].append(data) or
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": data}))
    # #317 : l'écriture ciblée contrôle le bail avant d'écrire. Ces tests portent
    # sur l'identité de la ligne visée, pas sur le verrou — elle y est libre.
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    def fusion(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : elle lit
        # la ligne que le test a posée (`datastore_get_row`, résolu à l'appel), et la
        # fusion aboutie est relevée comme l'était l'UPDATE.
        ligne = dsm.db.datastore_get_row(ns_id, rid)
        if ligne is None:
            return None
        data = apply_fn(dict(ligne.get("data") or {}))
        calls["update"].append((rid, data))
        return ({"row_id": rid, "created_at": "t", "updated_at": updated_at,
                 "data": data}, data)

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion)
    return st, calls


def test_the_founding_case_renamed_field_is_refused(store):
    """L'ancien nom n'est plus accepté en silence : refusé, nommé, rien d'écrit."""
    st, calls = store
    with pytest.raises(ColonneNonDeclaree) as e:
        st.append_row("v", {"siren": "1", "actualite_sociale": "…"})
    assert "`actualite_sociale`" in str(e.value) and "data_patch_schema" in str(e.value)
    assert calls["insert"] == []


def test_conforming_write_reports_nothing(store):
    """Pas de clé parasite dans la réponse quand tout est dans le format."""
    st, _ = store
    st.append_row("v", {"siren": "1", "analyse1": "a"})
    assert st.off_schema_report() == {}


def test_batch_is_refused_whole_before_its_first_row(store):
    st, calls = store
    with pytest.raises(ColonneNonDeclaree) as e:
        st.write_rows("v", [{"siren": "1", "analyse1": "a"},
                            {"siren": "2", "actualite_sociale": "b"},
                            {"siren": "3", "priorites_rh": "c"}])
    assert "`actualite_sociale`, `priorites_rh`" in str(e.value)
    assert calls["insert"] == []


def test_patch_judges_only_the_keys_written(store, monkeypatch):
    """Une colonne hors schéma DÉJÀ en base ne bloque pas le patch d'un autre champ
    (même raison que la borne max_length, #383) : le refus porte sur ce que le geste
    POSE."""
    st, calls = store
    monkeypatch.setattr(dsm.db, "datastore_get_row",
                        lambda ns_id, rid: {"row_id": rid, "created_at": "t",
                                            "updated_at": "t",
                                            "data": {"siren": "1",
                                                     "actualite_sociale": "vieux"}})
    st.update_row("v", "r1", {"analyse1": "propre"})
    assert calls["update"][-1][1]["analyse1"] == "propre"
    with pytest.raises(ColonneNonDeclaree, match="`priorites_rh`"):
        st.update_row("v", "r1", {"priorites_rh": "encore hors format"})
    assert len(calls["update"]) == 1


def test_refused_row_reports_nothing(store, monkeypatch):
    """Une écriture refusée n'a rien posé — le relevé ne parle que d'écritures
    acceptées."""
    st, calls = store
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": {
                            "fields": [{"key": "siren", "type": "text",
                                        "required": True}]}})
    with pytest.raises(RowValidationError):
        st.append_row("v", {"champ_inconnu": "x"})
    assert calls["insert"] == []
    assert st.off_schema_report() == {}
