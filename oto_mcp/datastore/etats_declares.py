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

**Les TRANSITIONS aussi — et elles n'ont rien coûté.** Ce module ne jugeait d'abord que
l'APPARTENANCE : une transition se juge entre deux valeurs, et seul l'état précédent de
la file était transmis. J'avais donc annoncé un lot séparé. Deux mesures l'ont rendu
inutile :

    100 des 132 colonnes secondaires déclarent des `transitions`  → inertes
    la ligne d'AVANT est DÉJÀ chargée par le chemin d'écriture    → rien à payer

Le chemin charge la ligne dès qu'un `lifecycle` existe, et n'en extrayait **qu'une
colonne**. Fermer ce cran n'ajoute aucune requête : il suffit de lire ce qui était là.

⚠️ **Sans `avant`, le contrôle de transition ne s'arme pas, silencieusement — et c'est
voulu.** Un appelant qui ne peut pas fournir l'état d'avant ne doit pas se voir refuser
une transition qu'on est incapable de juger. L'appartenance, elle, reste vérifiée.
"""
from __future__ import annotations

from typing import Optional

from .couches import unwrap
from .cycle_de_vie import refus_de_transition
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
                 gelees: Optional[list] = None,
                 avant: Optional[dict] = None) -> list[str]:
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
            # L'état est connu : reste à savoir si on avait le DROIT d'y aller.
            if etats and str(valeur) in etats:
                errors.extend(_transition_refusee(f, cle, valeur, avant))
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


def _transition_refusee(f: dict, cle: str, valeur, avant: Optional[dict]) -> list[str]:
    """Le passage d'un état à l'autre est-il déclaré permis ?

    ⚠️ **La valeur d'AVANT se déballe elle aussi.** Dès la deuxième écriture la ligne
    porte des couches : le cas normal est un objet, pas un mot. C'est le défaut qui a
    arrêté une campagne le 29/08 — deux gestes voisins qui lisent la même colonne
    doivent la lire pareil.
    """
    if not isinstance(avant, dict):
        return []
    transitions = f["lifecycle"].get("transitions")
    if not isinstance(transitions, dict) or not transitions:
        return []
    ancien = unwrap(avant.get(cle))
    if ancien is None or str(ancien) == str(valeur):
        return []
    permis = {str(t) for t in (transitions.get(str(ancien)) or [])}
    if str(valeur) in permis:
        return []
    return [refus_de_transition(str(cle), str(ancien), str(valeur), sorted(permis))]
