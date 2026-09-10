"""`hidden` et `width` sont prescrites par le produit — les dénoncer était une faute.

La description de `data_set_schema` les PRESCRIT, mot pour mot : « `width`… **declare
it** to keep a stable layout », « `hidden: true`… **Use it** for opaque ids and technical
fields ». L'avertissement des clés non interprétées les dénonçait pourtant à chaque
lecture de schéma. **Le produit disait « déclare-la », puis criait sur la déclaration.**

⚠️ **Ce que ça coûtait n'est pas le bruit, c'est le SIGNAL qu'il couvrait.** Mesuré le
10/09/2026 sur les 363 tableaux à schéma du parc :

    portaient un avertissement à la lecture   224   (61 %)
      dont à cause de `hidden`                186
      dont à cause de `width`                 141
    après ce correctif                         30   ( 8 %)

**194 tableaux rendus silencieux, et 87 % du bruit venait d'une seule cause.** Les 26
tableaux portant `enum` là où `options` fait foi — la clé même qui a laissé passer 504
valeurs libres — étaient noyés dedans.

⚠️ **Vérifié dans `oto-dashboard` AVANT de les déclarer**, parce qu'une clé « front »
que personne ne lirait serait la même faute dans l'autre sens : `hidden` filtre les
cartes et pilote la sauvegarde de vue, `width` est lu par le tiroir de fiche. Elles sont
donc dans le cas de `label` — interprétées par le consommateur auquel elles s'adressent,
jamais par le validateur.
"""
from __future__ import annotations

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore import schema_keys as sk

PRESCRITES = ("hidden", "width")


def test_elles_sont_declarees_FRONT_et_pas_validateur():
    """⚠️ La nuance porte tout : le serveur ne les applique pas, et le prétendre serait
    le défaut inverse. Elles sont lues en aval, comme `label`."""
    for cle in PRESCRITES:
        assert cle in sk.RECONNUES, f"`{cle}` est prescrite par le produit"
        assert cle in sk.LUES_PAR_LE_FRONT, f"`{cle}` est lue par le front"
        assert cle not in sk.LUES_PAR_LE_VALIDATEUR, (
            f"`{cle}` ne contraint RIEN côté serveur — la déclarer appliquée serait "
            f"la même faute, dans l'autre sens")


def test_un_schema_qui_les_porte_ne_declenche_plus_rien():
    """Le cas des 194 tableaux : ils suivaient la consigne et recevaient un reproche."""
    S = {"fields": [{"key": "ident", "type": "text", "hidden": True, "width": "half"}]}
    assert dsv2.unknown_declaration_keys(S) == [], "aucune clé à dénoncer"
    # ⚠️ La fonction rend `""` (faux) et non `None` quand il n'y a rien à dire : on
    # juge la VÉRACITÉ, pas l'identité — sinon le banc rougirait sur une convention.
    assert not dsv2.unknown_keys_read_warning(dsv2.unknown_declaration_keys(S))


def test_le_SIGNAL_survit_a_la_desaturation():
    """⚠️ L'autre moitié de l'exigence, et elle compte autant : un correctif qui aurait
    fait taire le bruit ET le signal aurait été pire que le défaut. `enum` à côté d'un
    `options` qui fait foi reste dénoncé — 26 tableaux du parc, et c'est la clé qui a
    laissé passer 504 valeurs libres."""
    S = {"fields": [{"key": "statut", "type": "enum",
                     "enum": ["a", "b"], "options": ["a", "b", "c"],
                     "hidden": True, "width": "full"}]}
    entrees = dsv2.unknown_declaration_keys(S)
    assert entrees, "un `enum` résiduel doit rester dénoncé"
    assert any((e.get("near_miss") or {}).get("enum") == "options" for e in entrees), (
        "et le near-miss doit dire laquelle des deux décide")
    assert dsv2.unknown_keys_read_warning(entrees), "l'avertissement doit être servi"


def test_ce_qui_reste_denonce_ne_contient_PAS_les_prescrites():
    """Le canal redevient lisible : ce qui parle encore est ce qui mérite d'être lu."""
    S = {"fields": [{"key": "x", "hidden": True, "width": "full", "zorglub": 1}]}
    dites = {k for e in dsv2.unknown_declaration_keys(S) for k in (e.get("keys") or [])}
    assert dites == {"zorglub"}, dites
