"""Datastore — `strict` descend DANS les éléments d'une colonne-liste (#544).

L'incident daté (29/08/2026, données réelles) : un tableau `strict: true` dont la
colonne `contacts` déclare ses attributs par `of.fields` a accepté, sur deux lignes
d'un rejeu, une clé `email_pattern` À L'INTÉRIEUR d'un contact — sans refus, sans
`hors_schema`, sans un mot. La veille, le même geste au PREMIER niveau avait été
interdit par consigne ; la nuit suivante il est reparu un cran plus bas, là où le
texte ne regardait pas. « Une interdiction protège la forme qu'elle décrit. »

La règle posée ici : un composite DÉCLARÉ (`object.fields`, `list.of.fields`) est un
référentiel FERMÉ — un attribut qu'il ne déclare pas est REFUSÉ, en nommant l'élément
(`contacts[1].email_pattern`). Depuis le 21/10/2026, sur tout tableau qui déclare au
moins une colonne (oto#124).

Ce qui ne change PAS, et chaque non-changement a son test :

- une liste dont le `of` ne déclare aucun champ — elle ne ferme rien, à tout étage ;
- une COUCHE d'attribut (`email.origine`) — c'est la forme SERVIE d'un item (oto#22
  §2), donc ce qu'un aller-retour lecture → écriture repose ;
- une ligne DÉJÀ porteuse d'un attribut hors format reste écritable par un patch qui
  ne la nomme pas (le gel de 23 lignes d'oto-backend#284, à ne pas rejouer).
"""
import pytest

from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.core import DatastorePg, RowValidationError


STRICT = {
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "occupant", "type": "object",
         "fields": [{"key": "nom", "type": "text"}]},
        {"key": "contacts", "type": "list",
         "of": {"type": "object", "fields": [
             {"key": "nom", "type": "text"},
             {"key": "fonction", "type": "text"},
             {"key": "email", "type": "email"}]}},
    ],
}

# Le contact de l'incident : tout est bon sauf une clé que `of.fields` ne déclare pas.
CONTACT_FAUTIF = {"nom": "B", "fonction": "DAF",
                  "email_pattern": "{prenom}.{nom}@x.fr"}


def _cherche(errors: list, fragment: str) -> str:
    trouve = [e for e in errors if fragment in e]
    assert trouve, f"aucune erreur ne nomme `{fragment}` — vu : {errors}"
    return trouve[0]


# ── le refus, et ce qu'il dit ────────────────────────────────────────────────

def test_lincident_une_cle_inconnue_dans_un_contact_est_refusee():
    errors = dsv2.validate_row(STRICT, {
        "siren": "1",
        "contacts": [{"nom": "A", "email": "a@x.fr"}, CONTACT_FAUTIF]})
    _cherche(errors, "contacts[1].email_pattern")


def test_le_refus_nomme_les_attributs_declares_et_le_geste():
    errors = dsv2.validate_row(STRICT, {"contacts": [CONTACT_FAUTIF]})
    msg = _cherche(errors, "contacts[0].email_pattern")
    for attendu in ("`nom`", "`fonction`", "`email`", "data_patch_schema"):
        assert attendu in msg, f"le refus ne dit pas {attendu} : {msg}"


def test_un_sous_champ_dobjet_declare_est_refuse_de_meme():
    errors = dsv2.validate_row(STRICT, {"occupant": {"nom": "ACME",
                                                     "naf": "62.01Z"}})
    _cherche(errors, "occupant.naf")


def test_la_fermeture_se_propage_vers_le_bas():
    """Un objet DANS un élément de liste reste fermé — sinon il suffirait
    d'enfouir la clé d'un cran de plus pour retrouver le silence de l'incident."""
    profond = {"fields": [
        {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
            {"key": "nom", "type": "text"},
            {"key": "adresse", "type": "object",
             "fields": [{"key": "ville", "type": "text"}]}]}}]}
    errors = dsv2.validate_row(profond, {"contacts": [
        {"nom": "A", "adresse": {"ville": "Marseille", "cedex": "8"}}]})
    _cherche(errors, "contacts[0].adresse.cedex")


def test_un_lot_de_contacts_ne_rend_pas_un_refus_par_element():
    """300 contacts fautifs ne font pas
    300 lignes de refus — un attribut nommé UNE fois, sur son premier élément."""
    errors = dsv2.validate_row(STRICT, {
        "contacts": [dict(CONTACT_FAUTIF) for _ in range(300)]})
    nommes = [e for e in errors if "email_pattern" in e]
    assert len(nommes) == 1
    assert "contacts[0].email_pattern" in nommes[0]


# ── ce qui passe, et doit continuer de passer ────────────────────────────────

def test_un_element_conforme_est_inchange():
    assert dsv2.validate_row(STRICT, {"siren": "1", "contacts": [
        {"nom": "A", "fonction": "DRH", "email": "a@x.fr"},
        {"nom": "B"}]}) == []


def test_une_couche_dattribut_nest_pas_un_attribut_inconnu():
    """La forme SERVIE d'un item aplatit ses couches (`email.origine`) : un
    aller-retour lecture → écriture les repose telles quelles."""
    assert dsv2.validate_row(STRICT, {"contacts": [
        {"nom": "A", "email": "a@x.fr", "email.origine": "hunter",
         "email.comment": "vérifié le 12/08"}]}) == []


def test_une_liste_dont_le_of_ne_declare_aucun_champ_reste_libre():
    libre = {"fields": [
        {"key": "notes", "type": "list", "of": {"type": "json"}},
        {"key": "fiches", "type": "list", "of": {"type": "object"}},
    ]}
    assert dsv2.validate_row(libre, {
        "notes": [{"quoi": "que ce soit"}],
        "fiches": [{"n_importe": 1, "quoi": 2}]}) == []


def test_un_tableau_sans_strict_ferme_aussi_ses_sous_records():
    """oto#124 : la fermeture ne dépend plus de `strict` — un tableau qui déclare au
    moins une colonne referme ses composites déclarés."""
    soft = {"fields": [
        {"key": "siren", "type": "text", "required": True},
        {"key": "contacts", "type": "list",
         "of": {"type": "object", "fields": [{"key": "nom", "type": "text"}]}}]}
    _cherche(dsv2.validate_row(soft, {"siren": "1", "contacts": [CONTACT_FAUTIF]}),
             "contacts[0].email_pattern")


def test_le_refus_ne_porte_que_sur_ce_que_le_geste_ECRIT():
    """Sinon une ligne déjà porteuse d'un attribut hors format deviendrait
    inécritable par n'importe quel patch, y compris sur un champ sans rapport —
    les 23 lignes gelées d'oto-backend#284, à ne pas rejouer."""
    merged = {"siren": "1", "contacts": [CONTACT_FAUTIF]}
    assert dsv2.validate_row(STRICT, merged, written={"siren"}) == []
    assert dsv2.validate_row(STRICT, merged, written={"contacts"})


# ── le seam d'écriture ───────────────────────────────────────────────────────

@pytest.fixture()
def store(monkeypatch):
    st = DatastorePg("u", acting_org=35)
    monkeypatch.setattr(st, "_resolve", lambda ns, write=False: 7)
    calls: dict = {"insert": []}
    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "schema": STRICT})
    monkeypatch.setattr(dsm.db, "datastore_insert_row",
                        lambda ns_id, rid, data, *a, **k: (
                            calls["insert"].append(data) or
                            {"row_id": rid, "created_at": "t", "updated_at": "t",
                             "data": data}))
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    return st, calls


def test_le_seam_decriture_refuse_et_necrit_rien(store):
    st, calls = store
    with pytest.raises(RowValidationError) as exc:
        st.append_row("v", {"siren": "1", "contacts": [CONTACT_FAUTIF]})
    assert "contacts[0].email_pattern" in str(exc.value)
    assert calls["insert"] == []
    assert st.off_schema_report() == {}


def test_le_seam_laisse_passer_un_contact_conforme(store):
    st, calls = store
    st.append_row("v", {"siren": "1",
                        "contacts": [{"nom": "A", "email": "a@x.fr"}]})
    assert calls["insert"][0]["contacts"] == [{"nom": "A", "email": "a@x.fr"}]
    assert st.off_schema_report() == {}
