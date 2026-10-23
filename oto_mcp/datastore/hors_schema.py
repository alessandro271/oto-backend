"""Une clé que la déclaration ne nomme pas, SOUS un composite déclaré — refusée.

Sous un composite DÉCLARÉ (`object.fields`, `list.of.fields`), quand le format fait
contrat (`validation_complete`), le référentiel est fermé : l'attribut que la
déclaration ne nomme pas est refusé en nommant l'élément fautif (`_unknown_subkeys`,
`_unknown_subkey_refusal`, #544). Au PREMIER niveau, une colonne non déclarée est
refusée sur tous les tableaux (oto#124) — c'est `colonnes_non_declarees`, qui
réutilise `couche_mal_ecrite`.

Y vit aussi `couche_mal_ecrite` : `champ.comentaire` n'est pas une clé inconnue de
plus, c'est une couche mal orthographiée — le dire change le geste de correction. Et
`types_geles_warning`, qui parle d'une ligne DÉJÀ hors format sans rien refuser :
l'écriture est passée, le fait est dit à celui qui traverse.

Ce qu'il ne tient pas :
- **les attributs de SCHÉMA non reconnus** (`zorglub: true` sur une colonne) →
  `cles_inconnues.py` et `vocabulaire.py` — ici on juge une LIGNE, pas un format ;
- **le refus d'une valeur mal typée** sur une clé bien déclarée → `validation.py` ;
- **le vocabulaire des couches** que `couche_mal_ecrite` compare → `couches.py`.
"""
from __future__ import annotations

from typing import Optional

from .couches import LAYER_KEYS, split_layer
from .declaration import _fields
from .phrases_de_refus import cle_la_plus_proche


def _unknown_subkeys(fields: list, data: dict) -> list[str]:
    """Les clés de `data` qu'aucun field de `fields` ne couvre — le prédicat UNIQUE
    du « hors du référentiel » d'un composite déclaré (`_row_errors`, #544).

    Deux règles, et elles valent à tous les étages :

    - **sans référentiel, rien n'est hors référentiel** : un composite qui ne déclare
      AUCUN field ne ferme rien (tout y serait « inconnu », ce qui n'informe
      personne) — c'est exactement le contrat d'une liste libre ;
    - **une COUCHE n'est pas un attribut** : la forme servie d'un item aplatit ses
      couches (`email.origine`, oto#22 §2), donc un aller-retour lecture → écriture
      les repose telles quelles. Les refuser casserait le geste le plus ordinaire.
    """
    declared = {f["key"] for f in fields
                if isinstance(f, dict) and isinstance(f.get("key"), str) and f["key"]}
    if not declared or not isinstance(data, dict):
        return []
    out = []
    for key in data:
        if key in declared:
            continue
        base, layer = split_layer(key)
        if layer and base in declared:
            continue
        out.append(key)
    return sorted(out)


def _unknown_subkey_refusal(path: str, fields: list) -> str:
    """Le refus d'un attribut non déclaré DANS un composite (#544).

    Il dit les trois choses qu'un agent doit savoir pour ne pas réessayer à
    l'identique : où (le chemin, RANG compris), ce qui était attendu (les attributs
    déclarés, dans leur ordre), et le geste."""
    noms = [f["key"] for f in fields
            if isinstance(f, dict) and isinstance(f.get("key"), str) and f["key"]]
    dispo = ", ".join(f"`{n}`" for n in noms)
    # oto#135 : la clé déclarée la plus proche, quand il y en a une — `emial` a une
    # destination, et le refus la dit au lieu de faire relire la liste.
    proche = cle_la_plus_proche(path.rpartition(".")[2], noms)
    dispo += f" (closest: `{proche}`)" if proche else ""
    return (f"{path}: undeclared attribute — the declared format is a contract and this "
            f"sub-record closes its attributes: {dispo}. Nothing was written. "
            "Write it under a "
            "declared name, or declare the attribute (`data_patch_schema`) then write again.")


def types_geles_warning(gelees: list) -> Optional[str]:
    """La phrase qui accompagne le relevé des types déjà hors format.

    ⚠️ **Ce n'est pas un reproche à l'appelant** : il n'a pas écrit ces colonnes, et
    son écriture a réussi. C'est un fait sur la LIGNE, dit à celui qui est en train
    de la toucher — le seul qui passera par là. Le refuser aurait gelé la ligne pour
    toujours ; le taire l'aurait laissée pourrir sans témoin."""
    if not gelees:
        return None
    noms = ", ".join(f"`{g['champ']}`" for g in gelees)
    return (f"your write went through. Report on ANOTHER part of the row: "
            f"{noms} — the value already stored there no longer respects the declared format. This "
            "is not your doing and nothing was refused: these columns were written "
            "before the format was set, or the format has changed since. As long "
            "as they stay this way, they display and read, but any "
            "declaration relying on them ignores them. To bring them back in line, "
            "write a conforming value there — the detailed refusal will tell you which one.")


def couche_mal_ecrite(cle: str, declarees) -> Optional[tuple]:
    """`effectif_comment` → `("effectif", "comment")` — ou `None` (oto#63).

    ⚠️ **La seule suggestion qu'on s'autorise, et elle n'est pas une devinette.** La
    règle du refus (`colonnes_non_declarees.refus`) reste entière : on ne pointe jamais la colonne
    « la plus proche », parce qu'une destination inventée envoie la valeur dans une
    colonne juste — pire qu'un refus sec (#678). Ici on ne cherche rien de proche : on
    RECONNAÎT une décomposition exacte. La clé refusée s'écrit `<colonne>_<couche>`,
    la couche est l'un des trois noms que le serveur connaît, et la colonne est
    DÉCLARÉE au schéma. Les trois conditions, sinon rien.

    Le geste qu'on répare : l'agent lit sa ligne réservée à plat, y voit
    `effectif.comment`, veut écrire un commentaire — et produit `effectif_comment`,
    parce qu'un point n'a pas l'air d'un nom de champ et que rien ne lui dit qu'il est
    adressable. Cinq colonnes fantômes sur une mission réelle sont exactement ça, et
    **toutes sont des `comment`**, jamais des origines ni des liens.

    ⚠️ **Une colonne inconnue étant refusée, cette faute ne crée plus une colonne
    invisible : elle fait perdre la fiche entière.** Le durcissement a
    transformé une perte silencieuse en perte totale sans que le défaut bouge — plus on
    durcit, plus il coûte, et c'est ce qui rend ce message urgent.

    ⚠️ Ambiguïté écartée par construction : un nom de colonne ne peut pas se terminer
    par `_comment` ET être déclaré, sans que ce soit précisément la colonne qu'on
    cherche. Si `effectif_comment` était elle-même déclarée, la clé ne serait pas
    refusée et on ne passerait pas ici.
    """
    for couche in LAYER_KEYS:
        suffixe = f"_{couche}"
        if cle.endswith(suffixe) and len(cle) > len(suffixe):
            colonne = cle[:-len(suffixe)]
            if colonne in declarees:
                return colonne, couche
    return None


def enveloppe_probable(schema: Optional[dict], data: dict) -> bool:
    """La ligne est-elle ENVELOPPÉE plutôt qu'écrite ? — aucune clé ne correspond.

    Sur les routes d'écriture, **le corps EST la ligne**. Qui suit la convention
    habituelle l'enveloppe dans un objet qui la nomme (`{"row": {…}}`) et fabrique une
    colonne réellement appelée `row`, contenant toute la ligne. Le refus de la colonne
    non déclarée le dirait, sans nommer l'enveloppe.

    ⚠️ **Le critère est l'ABSENCE TOTALE de correspondance**, pas la présence d'une
    clé inconnue (celle-ci est l'affaire de `colonnes_non_declarees`) : une ligne qui
    ne touche **pas une seule** des colonnes déclarées.

    ⚠️ **Mesuré avant d'être posé, sur la base servie** : 67 980 lignes de tableaux à
    colonnes déclarées, **une seule** ne correspondait à rien — sur un tableau qui en
    compte 1 986 et déclare huit colonnes. Le critère ne décrit donc aucun régime
    normal : il ne nomme que l'accident.

    Rend `False` dès qu'il manque de quoi juger — schéma sans colonnes déclarées
    (tableau libre), ligne vide, données non exploitables : on n'invente pas un refus
    sur une absence d'information.
    """
    if not isinstance(data, dict) or not data:
        return False
    declarees = {f["key"] for f in _fields(schema)
                 if isinstance(f.get("key"), str) and f["key"]}
    if not declarees:
        return False                      # tableau libre : rien à quoi comparer
    posees = {k for k in data if isinstance(k, str) and not k.startswith("_")}
    return bool(posees) and not (posees & declarees)
