"""Une colonne qui déclare ses états les fait respecter — même si ce n'est pas la file.

**Ce qui manquait.** Un tableau peut porter deux avancements pour deux acteurs : la
FILE que drainent les agents (`statut`, avec `claimable`/`max_claims`/`abandon_state`)
et les états d'un humain (`suivi`, qui ne porte que `states` et `terminal`). Depuis le
08/09/2026 les deux sont légitimes — mais seule la file voyait sa valeur vérifiée. La
seconde colonne était stockée, servie, lue par son consommateur, et **personne ne
contrôlait son contenu** : un état hors de sa propre liste y entrait sans un mot.

**Mesuré sur le parc entier le 09/09/2026, avant d'écrire une ligne :**

    131 colonnes portent un cycle de vie sans être la file
    131 déclarent une liste d'états          → il y a bien quelque chose à valider
      0 valeur hors de cette liste           → sur 8 646 cellules pleines examinées

⚠️ **Le zéro est le sujet, pas un détail.** Il ne dit pas que la garde est inutile : il
dit qu'elle est **gratuite**. Aucune écriture existante ne devient refusée ; la porte se
ferme avant que quelqu'un la pousse. C'est le même cas que les six types dont la
déclaration ne coûtait rien à faire respecter.

⚠️ **Et le zéro a été vérifié comme un zéro, pas cru comme un résultat** : la mesure
compte les lignes lues (9 421) et les cellules pleines examinées (8 646). Une sonde qui
n'aurait rien regardé aurait rendu le même zéro.

**Ce que ce module ne fait PAS, et il faut le savoir avant de s'y fier.** Il juge
l'APPARTENANCE d'un état à sa liste, jamais la TRANSITION. Une transition se juge entre
deux valeurs, et l'état précédent n'est transmis que pour la colonne de file
(`prev_status`) — le chemin d'écriture ne le relit pas pour les autres. Sur une colonne
secondaire, passer directement de `nouveau` à `signé` sans étape intermédiaire reste
donc permis, même si `transitions` l'interdit. Fermer ce cran demande de charger la
ligne d'avant ; ce n'est pas un oubli, c'est un lot séparé.
"""
from __future__ import annotations

from typing import Optional

from .couches import unwrap
from .declaration import _fields, status_field


def colonnes_a_etats(schema: Optional[dict], *, sauf: Optional[str] = None) -> list[dict]:
    """Les colonnes qui déclarent une liste d'états — hors celle passée en `sauf`.

    `sauf` sert à ne pas juger deux fois la colonne de FILE : elle est déjà traitée
    par `validate_row`, qui lui applique EN PLUS le contrôle de transition. Deux
    contrôles sur la même valeur rendraient deux fois le même refus, et un agent qui
    lit deux fois la même phrase croit à deux fautes.
    """
    out = []
    for f in _fields(schema):
        cle, lc = f.get("key"), f.get("lifecycle")
        if not isinstance(cle, str) or not isinstance(lc, dict):
            continue
        if sauf is not None and cle == sauf:
            continue
        if [s for s in (lc.get("states") or []) if isinstance(s, (str, int))]:
            out.append(f)
    return out


def etats_trahis(schema: Optional[dict], merged: dict, *,
                 written: Optional[set] = None,
                 gelees: Optional[list] = None) -> list[str]:
    """Les colonnes SECONDAIRES dont la valeur n'est pas dans leur propre liste.

    Le partage `posé / gelé` est celui de la colonne de file, et pour la même raison
    (07/09/2026) : « cet état est-il permis » juge une VALEUR, donc ce que l'appel
    écrit. Un état stocké devenu invalide — parce qu'on l'a retiré de la liste depuis —
    gèlerait sinon la ligne entière, y compris pour une écriture sans rapport, et pour
    toujours. Il est signalé (`gelees`) au lieu d'être refusé.

    ⚠️ La valeur se DÉBALLE. Une cellule vaut `{"valeur": …, "comment": …}` en base :
    juger la structure au lieu de son contenu est le défaut qui a arrêté une campagne
    entière le 29/08 sur la colonne d'état — le contrôle refusait « état inconnu » sur
    la ligne que la plateforme venait elle-même de compléter.
    """
    if not isinstance(merged, dict):
        return []
    sf = status_field(schema)
    file_key = sf.get("key") if isinstance(sf, dict) else None

    errors: list[str] = []
    for f in colonnes_a_etats(schema, sauf=file_key):
        cle = f["key"]
        if cle not in merged:
            continue
        valeur = unwrap(merged.get(cle))
        if valeur is None or valeur == "":
            continue
        etats = {str(s) for s in (f["lifecycle"].get("states") or [])}
        if not etats or str(valeur) in etats:
            continue
        refus = (f"{cle}: état inconnu {valeur!r} — cette colonne déclare ses états "
                 f"(états: {sorted(etats)}). Elle n'est pas la file de travail du "
                 f"tableau, mais elle dit ce qu'elle accepte, et c'est ce qui est "
                 f"appliqué ici.")
        if written is None or cle in written:
            errors.append(refus)
        elif gelees is not None:
            gelees.append({"champ": str(cle), "refus": refus})
    return errors
