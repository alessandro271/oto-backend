"""`scripts/durcir_schemas.py` — ranger le vocabulaire des schémas existants, une fois,
avant la fermeture (oto#34, #35, #127).

Deux moitiés : le PLAN, pur (ce que le rangement fait à un schéma), et le PASSAGE sur
une base (à blanc par défaut, `--appliquer` pour écrire, par le chemin normal de pose).
"""
from __future__ import annotations

import copy
import uuid

import pytest

from oto_mcp.datastore import schema_keys as K
from oto_mcp.datastore.definition import validate_schema_def
from scripts import durcir_schemas as M


# ── le plan, pur ─────────────────────────────────────────────────────────────

def test_les_textes_d_aide_se_replient_dans_description_dans_l_ordre():
    plan = M.durcir({"fields": [{"key": "a", "description": "Déjà là",
                                 "placeholder": "p", "hint": "h", "help": "Déjà là",
                                 "note": "n"}]})
    a = plan.schema["fields"][0]
    assert a["description"] == "Déjà là\nn\nh\np", "description en tête, puis l'ordre"
    assert not set(K.TEXTES_D_AIDE) & set(a)
    assert plan.repliees == [("fields.a", ["note", "help", "hint", "placeholder"])]


def test_enum_supprimee_a_cote_d_options_renommee_sinon():
    plan = M.durcir({"fields": [
        {"key": "pareil", "type": "enum", "options": ["a"], "enum": ["a"]},
        {"key": "autre", "type": "enum", "options": ["a", "b"], "enum": ["a"]},
        {"key": "seule", "type": "enum", "enum": ["x", "y"], "label": "S"}]})
    pareil, autre, seule = plan.schema["fields"]
    assert "enum" not in pareil and pareil["options"] == ["a"]
    assert "enum" not in autre and autre["options"] == ["a", "b"], "options fait foi"
    assert seule == {"key": "seule", "type": "enum", "options": ["x", "y"], "label": "S"}
    assert list(seule) == ["key", "type", "options", "label"], "renommée EN PLACE"
    cas = dict(plan.enum)
    assert cas["fields.pareil"].endswith("identique")
    assert "DIFFÉRENTE" in cas["fields.autre"]
    assert cas["fields.seule"] == "renommée en `options`"


def test_origine_et_semantic_search_sont_supprimees():
    plan = M.durcir({"semantic_search": True, "fields": [
        {"key": "a", "origine": "system"}]})
    assert plan.schema == {"fields": [{"key": "a"}]}
    assert {(c, k) for c, k, _ in plan.supprimees} == {
        ("", "semantic_search"), ("fields.a", "origine")}


def test_le_reste_va_dans_meta_au_MEME_niveau_et_reste_inerte():
    schema = {"stricte": True, "fields": [
        {"key": "a", "type": "text", "read_only": True, "labels": {"x": "X"}},
        {"key": "o", "type": "object", "fields": [
            {"key": "y", "display": "title", "depends_on": ["a"]}]},
        {"key": "l", "type": "list", "of": {"type": "text", "max_length": 3}},
        {"key": "s", "lifecycle": {"states": ["a"], "initial": "a"}}]}
    plan = M.durcir(schema)
    t = plan.schema
    assert t["meta"] == {"stricte": True}
    assert t["fields"][0]["meta"] == {"read_only": True, "labels": {"x": "X"}}
    assert "readonly" not in t["fields"][0], "jamais convertie en clé active"
    assert t["fields"][1]["fields"][0]["meta"] == {"display": "title",
                                                   "depends_on": ["a"]}
    assert t["fields"][2]["of"] == {"type": "text", "meta": {"max_length": 3}}
    assert t["fields"][3]["lifecycle"] == {"states": ["a"], "meta": {"initial": "a"}}
    assert validate_schema_def(t) == [], "le schéma rangé passe le refus"
    assert M.durcir(t).vide, "idempotent"


def test_un_schema_propre_n_a_rien_a_faire():
    assert M.durcir({"fields": [{"key": "a", "label": "A"}]}).vide
    assert M.durcir(None).vide


@pytest.mark.parametrize("champ,raison", [
    ({"key": "a", "meta": "x", "editable": True}, "n'est pas un objet"),
    ({"key": "a", "meta": {"editable": False}, "editable": True}, "autre valeur"),
    ({"key": "a", "explained_by": "é" * K.META_MAX_OCTETS}, "au-delà de la borne"),
])
def test_ce_qui_demande_un_arbitrage_n_est_pas_range(champ, raison):
    with pytest.raises(M.Inmigrable, match=raison):
        M.durcir({"fields": [champ]})


# ── le passage, sur une base ─────────────────────────────────────────────────

SUB = "u-durcir"

ANCIEN = {"strict": True, "semantic_search": True, "fields": [
    {"key": "nom", "type": "text", "note": "Le nom légal", "editable": True},
    {"key": "statut", "type": "enum", "enum": ["a", "b"], "origine": "system"}]}


def _tableau(schema) -> int:
    from oto_mcp import db
    ns_id = db.create_datastore("user", SUB, "durcir-" + uuid.uuid4().hex[:6])
    db.set_datastore_schema(ns_id, schema)
    return ns_id


def _passage(ns_ids, appliquer):
    lignes: list[str] = []
    bilan = M.executer(appliquer=appliquer, tableaux=ns_ids, sortie=lignes.append)
    return bilan, "\n".join(lignes)


def test_a_blanc_rien_n_est_ecrit_et_tout_est_dit(live):
    from oto_mcp import db
    ns_id = _tableau(copy.deepcopy(ANCIEN))
    bilan, texte = _passage([ns_id], appliquer=False)
    assert db.get_datastore_by_id(ns_id)["schema"] == ANCIEN, "à blanc = rien d'écrit"
    assert bilan["a_durcir"] == 1 and bilan["ecrits"] == 0
    assert "repliées dans description : fields.nom (note)" in texte
    assert "rangées dans meta : fields.nom (editable)" in texte
    assert "enum : fields.statut — renommée en `options`" in texte
    assert "À BLANC : 1 tableau(x) à durcir" in texte


def test_appliquer_ecrit_par_la_pose_journalise_et_un_second_passage_ne_trouve_rien(
        live):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns_id = _tableau(copy.deepcopy(ANCIEN))
    db.datastore_insert_row(ns_id, "r1", {"nom": "ACME", "statut": "z"})
    bilan, texte = _passage([ns_id], appliquer=True)
    assert bilan["ecrits"] == 1, texte
    assert db.get_datastore_by_id(ns_id)["schema"] == {"strict": True, "fields": [
        {"key": "nom", "type": "text", "description": "Le nom légal",
         "meta": {"editable": True}},
        {"key": "statut", "type": "enum", "options": ["a", "b"]}]}
    # `options` désormais ARMÉE sur un tableau strict : la pose dit ce qu'elle condamne.
    assert "→ écrit — la pose dit :" in texte and "`statut`" in texte
    with _connect() as conn:
        journal = conn.execute(
            "SELECT sub, args FROM tool_calls WHERE tool = 'data_set_schema' "
            "AND args->>'datastore' = %s", (str(ns_id),)).fetchall()
    assert len(journal) == 1 and journal[0]["sub"] is None
    assert journal[0]["args"]["migration_systeme"] == M.MIGRATION
    second, texte2 = _passage([ns_id], appliquer=False)
    assert second["a_durcir"] == 0, texte2


def test_un_tableau_a_arbitrer_est_liste_et_jamais_ecrit(live):
    from oto_mcp import db
    schema = {"fields": [{"key": "a", "meta": "pas un objet", "editable": True}]}
    ns_id = _tableau(schema)
    bilan, texte = _passage([ns_id], appliquer=True)
    assert bilan["inmigrables"] and "À ARBITRER" in texte
    assert db.get_datastore_by_id(ns_id)["schema"] == schema


def test_un_schema_qui_a_bouge_depuis_l_inventaire_est_saute(live, monkeypatch):
    from oto_mcp import db
    ns_id = _tableau(copy.deepcopy(ANCIEN))
    vrai = M.inventaire

    def _inventaire_perime(tableaux=None):
        lus = vrai(tableaux)
        db.set_datastore_schema(ns_id, {**ANCIEN, "key": "nom"})   # un geste passe
        return lus
    monkeypatch.setattr(M, "inventaire", _inventaire_perime)
    bilan, texte = _passage([ns_id], appliquer=True)
    assert bilan["bouges"] == [ns_id] and "SAUTÉ" in texte
    assert db.get_datastore_by_id(ns_id)["schema"]["key"] == "nom", "rien d'écrasé"


# ── les schémas cibles des slots de procédure ────────────────────────────────

PROPRIO = "u-durcir-proc"

SLOTS_ANCIENS = [
    {"name": "vivier", "type": "tableau", "description": "le vivier",
     "schema": {"key": "siren", "fields": [
         {"key": "siren", "type": "text", "help": "9 chiffres", "editable": False},
         {"key": "statut", "type": "enum", "enum": ["a", "b"]}]}},
    {"name": "crm", "type": "connecteur", "connector": "attio"},
]


def _procedure(slug: str, slots=None) -> dict:
    """Une procédure posée AVANT la fermeture : ses slots entrent tels quels, sans
    passer par `validate_slots` — c'est l'état du parc qu'on range."""
    from oto_mcp import org_store
    org_store.set_instruction("user", PROPRIO, slug, "1. Qualifier le vivier.",
                              title="Qualif", slots=copy.deepcopy(slots or SLOTS_ANCIENS),
                              set_by="u-auteur")
    return org_store.get_instruction("user", PROPRIO, slug)


def _passage_proc(ids, appliquer):
    lignes: list[str] = []
    bilan = M.executer(appliquer=appliquer, procedures=ids, sortie=lignes.append)
    return bilan, "\n".join(lignes)


def test_a_blanc_une_procedure_est_listee_slot_par_slot_et_rien_n_est_ecrit(live):
    from oto_mcp import org_store
    p = _procedure("qualif-blanc-" + uuid.uuid4().hex[:4])
    bilan, texte = _passage_proc([p["id"]], appliquer=False)
    assert bilan["procedures_a_durcir"] == 1 and bilan["procedures_ecrites"] == 0
    assert "slot `vivier`" in texte
    assert "repliées dans description : fields.siren (help)" in texte
    assert "rangées dans meta : fields.siren (editable)" in texte
    assert "enum : fields.statut — renommée en `options`" in texte
    assert "1 procédure(s) à durcir sur 1 à slot schématisé" in texte
    assert org_store.get_instruction("user", PROPRIO, p["slug"])["version"] == 1


def test_appliquer_ecrit_une_VERSION_et_garde_l_historique(live):
    from oto_mcp import org_store
    p = _procedure("qualif-ecrit-" + uuid.uuid4().hex[:4])
    bilan, texte = _passage_proc([p["id"]], appliquer=True)
    assert bilan["procedures_ecrites"] == 1, texte
    apres = org_store.get_instruction("user", PROPRIO, p["slug"])
    assert apres["version"] == 2 and apres["body_md"] == p["body_md"]
    assert apres["title"] == "Qualif"
    vivier = next(s for s in apres["slots"] if s["name"] == "vivier")
    assert vivier["schema"]["fields"][0] == {
        "key": "siren", "type": "text", "description": "9 chiffres",
        "meta": {"editable": False}}
    assert vivier["schema"]["fields"][1]["options"] == ["a", "b"]
    assert next(s for s in apres["slots"] if s["name"] == "crm") == SLOTS_ANCIENS[1]
    versions = org_store.list_instruction_versions("user", PROPRIO, p["slug"])
    assert {v["version"] for v in versions} >= {1, 2}
    assert org_store.get_instruction("user", PROPRIO, p["slug"], 1)["slots"] == \
        SLOTS_ANCIENS, "l'historique garde l'ancienne forme, restaurable"
    assert next(v for v in versions if v["version"] == 2)["set_by"] == M.AUTEUR
    second, _ = _passage_proc([p["id"]], appliquer=False)
    assert second["procedures_a_durcir"] == 0


def test_une_procedure_RETIREE_est_listee_jamais_ecrite(live):
    from oto_mcp import org_store
    from oto_mcp.db._conn import _connect
    p = _procedure("qualif-retiree-" + uuid.uuid4().hex[:4])
    with _connect() as conn:
        conn.execute("UPDATE org_instructions SET archived_at = NOW() WHERE id = %s",
                     (p["id"],))
    bilan, texte = _passage_proc([p["id"]], appliquer=True)
    assert bilan["procedures_retirees"] == [p["id"]] and "RETIRÉE" in texte
    assert org_store.get_instruction("user", PROPRIO, p["slug"])["version"] == 1


def test_une_procedure_qui_a_bouge_est_sautee(live, monkeypatch):
    from oto_mcp import org_store
    p = _procedure("qualif-bouge-" + uuid.uuid4().hex[:4])
    vrai = M.inventaire_procedures

    def _perime(procedures=None):
        lus = vrai(procedures)
        org_store.set_instruction("user", PROPRIO, p["slug"], "2. Autre corps.")
        return lus
    monkeypatch.setattr(M, "inventaire_procedures", _perime)
    bilan, texte = _passage_proc([p["id"]], appliquer=True)
    assert bilan["procedures_bougees"] == [p["id"]] and "SAUTÉE" in texte
    assert org_store.get_instruction("user", PROPRIO, p["slug"])["body_md"] == \
        "2. Autre corps.", "rien d'écrasé"
