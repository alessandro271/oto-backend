"""La file d'indexation sémantique : réservations, échecs et consommation du jour.

Deux tables NEUVES, nées entières : `embed_sources` et `embed_consommation`
(`db/schema/embeddings.py::EMBED_FILE`) — posées aussi par le démarrage
(`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL. Aucune FK, aucune colonne de
compte. Écrites et lues par `embed_worker` (`db/embed_file.py`).

**Verrous.** Aucun sur une table existante.

**Ordre avec le code : avant la remise en route du worker.** Le code de ce lot lit
`embed_sources` dans la sélection des sources à indexer : la table doit exister avant
que le worker tourne (démarrage du backend ou cette révision). L'ancien code l'ignore.

Retour arrière : retire les deux tables (les échecs notés et le compte du jour se
perdent ; rien d'autre).

Révision : 20261123_1433_embed_file
Précédente : 20261120_2240_docs_par_genre
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.embeddings import EMBED_FILE

revision = "20261123_1433_embed_file"
down_revision = "20261120_2240_docs_par_genre"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(EMBED_FILE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS embed_consommation")
    op.execute("DROP TABLE IF EXISTS embed_sources")
