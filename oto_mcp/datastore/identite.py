"""L'IDENTITÉ d'un tableau dans une réponse : son NUMÉRO et son NOM canonique.

Deux défauts fermés ensemble, parce qu'ils vivent dans la même clé.

**1. Rien ne donnait le numéro à l'agent.** Le tableau s'adresse indifféremment par
son nom ou par son numéro — `db.resolve_datastore_ns` accepte les deux, avec le
MÊME prédicat de visibilité — mais aucune remise ne rendait le numéro : un agent qui
ne l'avait pas lu dans `data_list_namespaces` ne pouvait pas l'employer. Le nom part
en retrait ; l'usage ne bascule que si le numéro arrive DANS la réponse, au moment où
l'agent en a besoin. D'où `ns_id`, servi à côté de `namespace`.

**2. `namespace` était un ÉCHO, pas une identité.** La réponse répétait la chaîne
reçue : adresser par nom rendait le nom, adresser par numéro rendait `"600"`. Elle
répondait donc à la question posée au lieu de dire quel tableau a été touché — et deux
appelants sur le même tableau lisaient deux valeurs différentes sans que rien ne le
dise. Ici, `namespace` est le nom canonique lu dans `user_datastores`, quelle que soit
la forme de l'adresse.

⚠️ **Changement de comportement assumé, et le seul** : un appelant qui adressait par
numéro (ou par `slot:<nom>`) et relisait `namespace` pour se vérifier reçoit désormais
le nom du tableau, pas sa propre chaîne. C'est ce que la clé prétendait dire.

Le nom de clé `ns_id` n'est pas neuf : c'est déjà celui de la résolution
(`DatastorePg._resolve`), du relevé de journal (`_trace`, `NsContext`), de la cible
d'un upload signé, du relevé d'appel MCP (`_TRACED_ARGS`) — et il est déjà SERVI aux
agents par `oto_search`, dont un hit `kind: "ligne"` porte `ref: {ns_id, row_id}`.
`id`, lui, était pris : dans une remise de ligne (`data_release`, `data_delete_row`)
il désigne la LIGNE, et réutiliser ce mot pour le tableau créerait un homonyme
exactement là où l'agent adresse.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

# Le nom de la clé, écrit une fois : les deux faces et les tests le citent d'ici.
CLE = "ns_id"

# La phrase servie AVEC la clé, au contrat comme aux descriptions d'outils : une clé
# neuve dont personne ne dit à quoi elle sert reste une clé que personne n'emploie.
DESCRIPTION = (
    "The table's NUMBER — the form to pass as `namespace` from here on. A name still "
    "resolves (same visibility check, no retirement date set), it is being retired, "
    "not broken. `null` only when no table was resolved.")


def identite(ns_id: Any = None, nom: Optional[str] = None, *,
             adresse: str = "") -> dict:
    """`{"namespace": <nom canonique>, "ns_id": <numéro>}` à fusionner dans une réponse.

    `adresse` = la chaîne que l'appelant a passée, gardée en DERNIER recours : un
    chemin qui a échoué avant de relever quoi que ce soit rend alors ce qu'il rendait
    avant, plutôt qu'un `null` à la place d'un nom. `ns_id` vaut `None` dans ce seul
    cas — sa présence est donc la preuve que le tableau a bien été résolu.
    """
    return {"namespace": nom or adresse,
            CLE: int(ns_id) if ns_id is not None else None}


def de_releve(releve: Optional[Mapping], adresse: str = "") -> dict:
    """La même, depuis un relevé `{ns_id, namespace}` — `DatastorePg.dernier_tableau`
    ou le `trace` d'une mutation, qui portent tous deux ces deux clés."""
    releve = releve or {}
    return identite(releve.get("ns_id"), releve.get("namespace"), adresse=adresse)


def numero(releve: Optional[Mapping]) -> dict:
    """Le NUMÉRO seul : `{"ns_id": …}`, sans le nom.

    Pour les remises dont le corps EST la ligne (`data_write`, `POST …/rows`) : elles
    ne servaient aucun `namespace`, il n'y a donc pas d'écho à corriger — et y poser
    une clé `namespace` à côté des colonnes de l'utilisateur mettrait un mot très
    plausible en collision avec une vraie colonne. `ns_id` s'ajoute, `namespace` non.
    """
    return {CLE: identite((releve or {}).get("ns_id"))[CLE]}
