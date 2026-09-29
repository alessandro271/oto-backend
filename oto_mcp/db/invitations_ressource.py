"""L'index du PARTAGE EN ATTENTE d'un objet (`oto_mcp/partage_en_attente.py`).

Un seul partage en attente par (objet, adresse) : deux clics simultanés ne font pas
deux secrets porteurs. L'échéance n'est pas dans le prédicat (`NOW()` n'est pas
immuable) : une ligne échue est retirée avant d'en poser une neuve.

HORS du DDL assemblé, comme `transcription.DDL_COLONNE_TRANSCRIPT` : le prédicat lit
`org_invitations.declined_at`, qu'un ALTER de `_init.py` pose APRÈS l'assemblage sur une
base existante (`docs/live-migrations.md`). `_init.py` l'exécute juste après cet ALTER ;
la révision `0028_partage_en_attente` l'exécute après les colonnes
(`schema/orgs.py::INVITATIONS_RESSOURCE`).
"""
from __future__ import annotations

DDL_INDEX_RESSOURCE_ATTENTE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_org_invitations_ressource_attente "
    "ON org_invitations (resource_kind, resource_id, lower(email)) "
    "WHERE resource_kind IS NOT NULL AND accepted_at IS NULL AND declined_at IS NULL")
