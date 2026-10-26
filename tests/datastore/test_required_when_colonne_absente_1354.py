"""#1354 (1) : une condition de `required_when` qui ne vise aucune colonne déclarée.

`{"field": "nature", "equals": "prévu"}` était accepté sans un mot : la condition se
lit sur des colonnes SŒURS (`data.get(<clé>)` à l'écriture), donc « requis quand la
colonne `field` vaut "nature" et la colonne `equals` vaut "prévu" » — deux colonnes
qui n'existent pas, une contrainte qui ne s'arme jamais. Même règle de famille que
#347 : une forme non interprétée se refuse à la pose, en nommant la forme attendue.
"""
from __future__ import annotations

from oto_mcp.datastore import schema as dsv2


def _schema(rw, *, avec_motif=True):
    champs = [{"key": "nature", "type": "text"}]
    if avec_motif:
        champs.append({"key": "motif", "type": "text", "required_when": rw})
    return {"fields": champs}


def test_la_forme_field_equals_se_refuse_en_nommant_la_forme_attendue():
    errs = dsv2.validate_schema_def(_schema({"field": "nature", "equals": "prévu"}))
    [err] = [e for e in errs if "required_when" in e]
    assert "`field`" in err and "`equals`" in err
    assert "{<colonne>: <valeur" in err


def test_une_condition_sur_une_colonne_declaree_passe():
    assert dsv2.validate_schema_def(_schema({"nature": "prévu"})) == []
    assert dsv2.validate_schema_def(_schema({"nature": ["prévu", "fait"]})) == []


def test_la_regle_vaut_aussi_dans_un_composite():
    schema = {"fields": [{"key": "lignes", "type": "list", "of": {"type": "object", "fields": [
        {"key": "nature", "type": "text"},
        {"key": "motif", "type": "text", "required_when": {"statut": "x"}}]}}]}
    errs = dsv2.validate_schema_def(schema)
    assert any("lignes.motif" in e and "`statut`" in e for e in errs), errs


def test_une_condition_heritee_du_schema_en_place_ne_bloque_pas_un_patch():
    """Un schéma vivant qui porte déjà la forme fautive reste patchable : seule une
    condition POSÉE par ce geste se juge (même règle que les clés inconnues)."""
    en_place = _schema({"field": "nature", "equals": "prévu"})
    patche = {"fields": en_place["fields"] + [{"key": "note", "type": "text"}]}
    assert dsv2.validate_schema_def(patche, en_place) == []
    # … mais la RE-poser autrement, c'est la poser : refusée.
    repose = _schema({"field": "nature"})
    assert any("required_when" in e for e in dsv2.validate_schema_def(repose, en_place))
