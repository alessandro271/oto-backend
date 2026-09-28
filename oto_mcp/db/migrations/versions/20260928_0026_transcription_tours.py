"""transcription_jobs.transcript : les tours verbatim d'une transcription (ADR 0074).

Une colonne `JSONB` NULLABLE, sans défaut : écriture de catalogue seule. Elle porte les
tours `[{speaker, start, end, text}]` que la face REST rend (`GET
/api/me/transcriptions/{id}`) ; la face MCP ne les rend jamais. À part de `result`
exprès : l'ancienne face MCP recopie `result` tel quel dans sa réponse, et le worker
tourne en préprod comme en prod sur la même file.

L'`ALTER` prend un `AccessExclusiveLock` sur `transcription_jobs`, que le worker lit
toutes les 5 s : d'où le `lock_timeout`. Le démarrage pose la même colonne s'il ne la
trouve pas (`transcription.DDL_COLONNE_TRANSCRIPT`, sous garde de catalogue) : révision
et boot sont idempotents l'un envers l'autre, l'ordre entre eux est indifférent. Une
base NEUVE la reçoit du `CREATE TABLE` (`db/schema/transcription.py`).

⚠️ Prod et préprod partagent la MÊME base. L'ancien code ne lit pas la colonne et ne
l'écrit pas (ses travaux terminés la laissent NULL, rendue `transcript: null`) : jouer
cette révision avant ou après le déploiement est sûr. Le retour arrière retire la
colonne et les tours qu'elle gardait ; le code qui l'écrit doit être retiré AVANT, sinon
chaque transcription terminée échoue à s'enregistrer.

Révision : 0026_transcription_tours
Précédente : 0025_repli_api
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.transcription import COLONNE_TRANSCRIPT, DDL_COLONNE_TRANSCRIPT

revision = "0026_transcription_tours"
down_revision = "0025_repli_api"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_TRANSCRIPT)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE transcription_jobs DROP COLUMN IF EXISTS {COLONNE_TRANSCRIPT}")
