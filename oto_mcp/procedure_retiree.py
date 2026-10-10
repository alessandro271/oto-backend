"""La procédure qu'un agent planifié ou webhook va dérouler est-elle RETIRÉE ?

Une procédure archivée sort des listes et cesse d'être proposée, mais un déclencheur
qui la nomme continuait de partir : son agent la lisait par `oto_procedure(op='get')`,
qui sert une procédure retirée (avec `archived_at`, pour qu'une routine qui la cite le
dise au lieu de casser), et la déroulait. Archiver ne suffisait donc pas à arrêter ce
qu'on retirait (signal #1374).

La procédure lue est celle que la cascade de lecture sert sans palier demandé : celle
du PROPRIÉTAIRE du déclencheur d'abord, puis celle de l'org du travail. Le palier
équipe n'est pas lu : un déclencheur ne porte pas d'équipe, et son agent ne l'atteint
que si son porteur en a une active — ce qu'aucune lecture ici ne peut savoir. Une
procédure d'équipe archivée n'est donc pas refusée ici ; l'agent la lit avec son
`archived_at`.
"""
from __future__ import annotations

from typing import Optional

#: Le code du refus, servi à l'appelant d'un webhook et nommé dans la description.
CODE = "procedure_archived"


def archivee(org_id: Optional[int], sub: Optional[str], slug: Optional[str]) -> Optional[str]:
    """La date de retrait (texte) de la procédure `slug` que l'agent lirait, ou None si
    elle est en service — ou introuvable : une procédure absente n'est pas « retirée »,
    et ce n'est pas à cette garde de le juger. Lecture en base : hors de la boucle."""
    from . import org_store
    if not slug:
        return None
    for otype, oid in (("user", sub), ("org", org_id)):
        if oid is None:
            continue
        row = org_store.get_instruction(otype, str(oid) if otype == "user" else int(oid), slug)
        if row:
            date = row.get("archived_at")
            return str(date) if date is not None else None
    return None
