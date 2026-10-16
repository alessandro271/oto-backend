"""Le mode de la clé d'un travail de transcription (signal oto 1400).

Une colonne NEUVE, nullable, sans défaut — le DDL est
`db/transcription.py::DDL_COLONNE_KEY_MODE`, exécuté tel quel :
`transcription_jobs.key_mode TEXT`. Le worker journalise la durée facturée d'un
travail hors de tout contexte d'appel : le mode de la clé résolue au dépôt doit donc
voyager avec le travail. NULL sur les travaux d'avant.

**Verrous.** `AccessExclusiveLock` bref sur `transcription_jobs` (colonne nullable sans
défaut : catalogue seul, aucune réécriture). `lock_timeout` 5 s : le worker tient des
lignes de la table pendant un appel à Mistral ; un échec net, qu'on rejoue, plutôt
qu'une attente qui bloquerait la file derrière elle.

**Ordre avec le code : indifférent.** Le démarrage pose la même colonne (`ADD COLUMN IF
NOT EXISTS`, même DDL) sous garde de catalogue.

Retour arrière : retire la colonne (le mode des travaux en cours se perd, leur durée se
journalise alors sans mode).

Révision : 0059_transcription_key_mode
Précédente : 0058_journal_des_prets
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.transcription import COLONNE_KEY_MODE, DDL_COLONNE_KEY_MODE

revision = "0059_transcription_key_mode"
down_revision = "0058_journal_des_prets"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(DDL_COLONNE_KEY_MODE)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(f"ALTER TABLE transcription_jobs DROP COLUMN IF EXISTS {COLONNE_KEY_MODE}")
