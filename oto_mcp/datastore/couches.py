"""Le vocabulaire des COUCHES d'une cellule, et les formes qu'elles prennent (#318).

Une colonne peut porter, à côté de sa `valeur`, trois couches qui la qualifient :
`origine` (le point de départ), `comment` (d'où vient ce qu'on affirme) et `link`
(l'URL qui atteste). Ce module tient ce vocabulaire fermé et les six gestes qui
l'entourent : reconnaître une cellule à couches (`names_layers`), en extraire la
valeur nue (`unwrap`), l'aplatir pour le service (`flat_layers`, `served_value`),
décomposer une adresse `champ.couche` (`split_layer`), lire une couche
(`layer_value`), et juger l'identité de deux valeurs (`same_value`) ou le vide
(`_is_empty`/`est_vide`).

**Ce module ne connaît AUCUN schéma, et c'est structurel.** Aucun attribut de
déclaration n'autorise ni n'interdit à une colonne de porter des couches : une
cellule dont la valeur est un objet portant `valeur` EN a, toute autre en est
dépourvue. Le contrat vaut pour toute colonne de tout tableau, à tous les étages.
C'est pourquoi il ne dépend de rien du domaine — il est le socle sur lequel les
autres modules s'appuient, jamais l'inverse.

Ce qu'il ne tient pas :
- **quelles couches une colonne EXIGE** (`required_layers`) → `couches_exigees.py` ;
- **la couche `origine` posée par la PLATEFORME** (le cran `origine: "system"`, son
  refus et son préavis) → `champs_reserves.py` ;
- **la forme imbriquée servie au lecteur** (`layers=nested`) → `layers.py` ;
- **le refus d'une couche mal orthographiée** → `hors_schema.py`.
"""
from __future__ import annotations

from typing import Any

# --- couches d'une colonne (#318) ---------------------------------------------
# NATIF et universel : aucune déclaration ne dit qu'une colonne porte des couches.
# Une colonne dont la valeur est un objet portant `valeur` EN a ; toute autre en est
# dépourvue. C'est le contrat, et il vaut pour toute colonne de tout tableau.
VALUE_LAYER = "valeur"
ORIGIN_LAYER = "origine"
# Trois couches, pas cinq. `source` et `commentaire` disaient la même chose — d'où
# `comment` seul ; `link` porte l'URL qui atteste, quand il y en a une.
# ⚠️ Conséquence assumée : `group_by champ.comment` ne comptera les provenances que
# si elles sont écrites de façon régulière (« registre », « déduction »). C'est
# possible, ce n'est plus induit par la forme.
LAYER_KEYS = (ORIGIN_LAYER, "comment", "link")

# Tout ce qu'une colonne à couches peut porter, valeur comprise.
ALL_LAYER_KEYS = (VALUE_LAYER, *LAYER_KEYS)

# Les couches qui décrivent LA VALEUR : elles la suivent, et disparaissent avec elle
# — les garder au-dessus d'une valeur remplacée ferait affirmer une provenance fausse.
# `origine` n'en est pas : elle décrit le point de DÉPART, pas la valeur courante, et
# c'est pourquoi elle est la seule à survivre à une réécriture.
VALUE_BOUND_LAYERS = tuple(k for k in LAYER_KEYS if k != ORIGIN_LAYER)

# La couche d'origine POSÉE PAR LE SYSTÈME (#586) : `{"key": "x", "origine": "system"}`
# au schéma. Vocabulaire fermé à UNE valeur — une origine « posée par l'agent » n'est
# pas une déclaration, c'est le défaut de départ (l'agent l'a réécrite une fois sur
# quarante et une, et c'était l'unique copie de la valeur remise).
SYSTEM_ORIGIN = "system"

#: Ce que porte `<champ>.origine` quand la valeur d'import n'est PAS connaissable :
#: la ligne existait déjà quand le format a été déclaré, et personne ne peut dire si
#: un agent l'a écrite entre-temps (oto#70).
#:
#: ⚠️ **Ce n'est pas une valeur, c'est l'aveu qu'il n'y en a pas.** Le guide le dit, et
#: le marqueur est écrit en clair — entre parenthèses — pour qu'un agent qui le lit sans
#: avoir lu le guide ne le prenne pas pour une donnée métier.
#:
#: Pourquoi pas la valeur courante, comme le faisait v1.207.0 : sur une ligne déjà
#: travaillée, cette valeur est celle d'un AGENT. La présenter comme origine, c'est
#: exactement ce que la définition interdit — et `A`, la vraie valeur d'import, est
#: perdue sans que rien ne le dise.
#:
#: Pourquoi pas « la ligne n'a pas bougé depuis sa création, donc sa valeur courante
#: EST son import » : `datastore_insert_row` accepte `created_at`/`updated_at` en
#: paramètres (override de backfill). Les comparer serait une HEURISTIQUE, et on ne
#: fonde pas la sémantique d'une donnée sur une heuristique.
ORIGINE_INCONNUE = "(origine inconnue)"


def same_value(a: Any, b: Any) -> bool:
    """Deux valeurs IDENTIQUES — au type près : `0` n'est pas `False`, `1` n'est pas
    `1.0`. Un seul juge pour « rien n'a changé », partout où ça décide (la fusion des
    couches, les champs réservés)."""
    return type(a) is type(b) and a == b


def names_layers(value: Any) -> bool:
    """L'écriture NOMME-t-elle des couches ? — un dict fait UNIQUEMENT de couches
    connues. Strict, comme tout écrivain : `{"a": 1, "origine": "x"}` reste une donnée
    `json` métier qui se trouve avoir un champ nommé « origine ». UN seul juge, pour
    la fusion (`columns._writes_layers`) comme pour les champs réservés
    (`reserved_refusals`) — deux copies divergeraient un jour sur un cas limite."""
    return (isinstance(value, dict) and bool(value)
            and all(k in ALL_LAYER_KEYS for k in value))


def unknown_layers(value: Any) -> list:
    """Couches d'une colonne que CETTE version du serveur ne connaît pas.

    L'asymétrie est le cœur du contrat d'évolution : le LECTEUR tolère (une couche
    écrite par une version plus récente est ignorée, la valeur reste lisible — sinon
    un déploiement progressif casserait les anciens nœuds), l'ÉCRIVAIN refuse. C'est
    ce qui permet d'ajouter une couche sans jamais dégrader l'ancien.

    Refuser à l'écriture plutôt que stocker en silence, parce qu'on a déjà payé
    l'inverse : une clé `enum:` posée là où le validateur lit `options:` a été
    acceptée, stockée, jamais lue — et 504 lignes ont été écrites en croyant le champ
    contraint. Une couche mal orthographiée doit s'apprendre à l'écriture, pas se
    découvrir six semaines plus tard.

    Un dict sans AUCUNE clé de couche connue n'est pas une colonne à couches :
    c'est une valeur `json` ordinaire, on n'y touche pas. ⚠️ Le critère a été
    corrigé par #329 — jusque-là le court-circuit exigeait `valeur`, si bien
    qu'un `{"origine": x, "sourse": y}` (le geste du rattrapage #326, une faute
    de frappe plus loin) passait SANS refus et écrasait la valeur existante en
    silence. La même validation s'applique désormais dans les deux cas."""
    if not isinstance(value, dict):
        return []
    connues = {VALUE_LAYER, *LAYER_KEYS}
    if not (set(value) & connues):
        return []
    return sorted(k for k in value if k not in connues)


def unwrap(value: Any) -> Any:
    """La VALEUR d'une colonne, qu'elle porte des couches ou non.

    Le pendant Python de l'expression SQL polymorphe — MÊME règle, deux endroits
    parce que deux langages, jamais deux règles. Tout ce qui JUGE une valeur (types,
    requis, bornes, options) doit déballer d'abord : sinon un schéma strict qui
    déclare `email` en `text` refuse un objet, et l'écriture en couches devient
    impossible précisément sur les tableaux qu'on recommande de rendre stricts.

    ⚠️ Conséquence assumée du caractère universel : un champ `json` légitime dont le
    contenu porte une clé `valeur` (`{"valeur": 42, "unite": "kg"}`) est déballé lui
    aussi. C'est le prix de « pas de déclaration » — la convention s'applique partout,
    y compris là où l'auteur ne pensait pas à elle. Le repli est bénin (on rend la
    valeur au lieu de l'objet, souvent ce qu'on voulait), et l'alternative — un
    marqueur réservé, ou une déclaration par colonne — rachèterait un cas rare au prix
    de la simplicité qui fait tout l'intérêt de la primitive."""
    if not isinstance(value, dict):
        return value
    if VALUE_LAYER in value:
        return value[VALUE_LAYER]
    # Pas de `valeur`, mais QUE des couches connues ⟹ c'est bien une colonne à
    # couches, dont la valeur n'est pas encore posée. Le cas nominal d'un import de
    # socle : on remplit `origine` sur un champ qu'aucun agent n'a renseigné. Sans
    # ça la lecture rendait l'OBJET — donc tout ce qui attend une chaîne cassait,
    # précisément sur le chemin qu'on recommande.
    if value and all(k in LAYER_KEYS for k in value):
        return None
    return value


def split_layer(field: str) -> tuple:
    """`email.comment` → `("email", "comment")` ; `email` → `("email", None)`.

    Ne coupe qu'au DERNIER point, et seulement si le suffixe est une couche connue :
    un champ légitimement nommé `taux.2024` reste un nom de colonne entier. Le
    vocabulaire est FERMÉ, donc l'ambiguïté est décidable — pas de devinette. La
    valeur, elle, se désigne par le nom NU (`email`), jamais `email.valeur` : c'est
    pourquoi `VALEUR_LAYER` n'est pas un suffixe coupable.

    ⚠️ Domicile ICI depuis #377, plus dans `db/paths` : la validation de schéma en a
    besoin, et `db.paths` importe déjà ce module — l'inverse ferait un cycle.
    `db.paths.split_layer` la ré-exporte, si bien que la grammaire reste à UN endroit
    pour le SQL comme pour le schéma. Deux copies, c'est le défaut qu'on a déjà payé :
    le même chemin répondait juste sur un verbe et faux sur trois."""
    base, sep, last = str(field).rpartition(".")
    if sep and base and last in LAYER_KEYS:
        return base, last
    return str(field), None


def layer_value(column: Any, layer: str) -> Any:
    """La valeur d'UNE couche d'une colonne — `None` si la colonne n'en porte pas.

    Une colonne écrite en scalaire (`"hors_perimetre"`) n'a aucune couche : sa
    justification est ABSENTE, et c'est bien ce qu'un requis doit constater. Sans ce
    `None`, il suffirait d'écrire la valeur nue pour échapper au motif."""
    return column.get(layer) if isinstance(column, dict) else None


def flat_layers(key: str, value: Any) -> dict:
    """Les couches RENSEIGNÉES d'une colonne, aplaties en `clé.couche`.

    Point unique : le premier niveau d'une ligne et les attributs d'un item de liste
    l'appellent tous les deux. Deux implémentations exposeraient deux formes de la
    même chose — et c'est le consommateur qui paierait la différence."""
    if not isinstance(value, dict) or not any(k in LAYER_KEYS for k in value):
        return {}
    return {f"{key}.{layer}": value[layer] for layer in LAYER_KEYS
            if value.get(layer) not in (None, "")}


def layer_address(name: Any):
    """L'INVERSE de `flat_layers` : `"site_web.comment"` → `("site_web", "comment")`.

    ⚠️ Elle vit ICI, collée à la fonction qu'elle inverse, parce que c'est la seule
    façon que les deux ne divergent pas : `flat_layers` fabrique `f"{clé}.{couche}"`,
    celle-ci le défait. **Ce qu'on sert doit pouvoir être réécrit tel quel** — et
    l'aller-retour se referme exactement là où ces deux-là s'accordent.

    Rend `None` dès que la forme n'est pas une adresse de couche, et les trois refus
    sont volontaires : un suffixe qui n'est pas une couche connue (`champ.inexistant`)
    n'en est pas une ; une base qui porte encore un point (`a.b.comment`) ne peut
    désigner aucune colonne, puisqu'un nom de colonne n'en porte jamais ; une base
    indexée (`contacts[0].email`) est un CHEMIN de lecture, pas une colonne.

    `valeur` n'en fait pas partie : `flat_layers` ne la sert jamais à plat (le nom nu
    la rend déjà), donc `champ.valeur` n'est le retour d'aucun aller."""
    if not isinstance(name, str) or "." not in name:
        return None
    base, _, couche = name.rpartition(".")
    if couche not in LAYER_KEYS or not base or "." in base or "[" in base:
        return None
    return base, couche


def served_value(value: Any) -> Any:
    """Ce qu'un LECTEUR reçoit pour cette colonne (oto#22 §1-2).

    `unwrap` rend la valeur d'UNE colonne ; celle-ci descend d'un cran quand cette
    valeur est une LISTE DE FICHES. Sans elle, la garantie « le nom nu rend la valeur,
    jamais la structure interne » se romprait au moment précis où les attributs d'un
    item adoptent des couches : `row["contacts"][0]["email"]` rendrait l'enveloppe
    au lieu de l'e-mail, donc tout consommateur casserait — silencieusement, le jour
    où quelqu'un pose une source sur un contact.

    Les couches d'un attribut sont aplaties DANS l'item (`item["email.origine"]`) :
    la règle du premier niveau, appliquée un cran plus bas, plutôt qu'un second
    vocabulaire à apprendre. Qui sait lire `row["email.origine"]` sait lire
    `item["email.origine"]`.

    Un item non-dict traverse tel quel — une liste de scalaires reste une liste de
    scalaires."""
    v = unwrap(value)
    if isinstance(v, list):
        return [_served_item(item) for item in v]
    return v


def _served_item(item: Any) -> Any:
    """Un item de liste est une FICHE : chacun de ses attributs est une feuille."""
    if not isinstance(item, dict):
        return item
    out: dict = {}
    for k, v in item.items():
        out[k] = served_value(v)
        out.update(flat_layers(k, v))
    return out


# ── validation d'une ROW à l'écriture ────────────────────────────────────────

def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


#: Alias PUBLIC de `_is_empty`. La notion de « vide » du datastore est UNE : un
#: appelant qui en écrirait une seconde la ferait diverger au premier cas limite
#: — c'est exactement le défaut de #608, où le validateur et le merge lisaient la
#: chaîne vide autrement l'un que l'autre.
est_vide = _is_empty
