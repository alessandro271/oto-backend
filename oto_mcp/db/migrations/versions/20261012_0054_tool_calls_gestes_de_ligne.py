"""tool_calls : l'index partiel des gestes d'UNE ligne de datastore — le parcours d'une ligne.

Le parcours d'une ligne (`usage.datastore_row_activity`, `GET
/api/datastores/{ns}/rows/{row_id}/activity`) cherche les gestes `data_*` qui portent
l'`id` de la ligne. Jusqu'ici, seul `idx_tool_calls_ns` (le tableau) le servait : sur un
tableau très actif, une ligne qui y a peu de gestes faisait parcourir tout l'historique
du tableau jusqu'à remplir la page. Mesuré en production (lecture seule, 12/10/2026) :
compter les gestes de 30 jours d'un seul tableau par cet index dépassait 15 s, et la
route rendait 503 à la borne de 10 s, trois fois en une minute.

`idx_tool_calls_ns_row` range les gestes par (tableau, ligne, date) : une lecture directe,
qui s'arrête à la page. Partiel sur les seuls gestes `data_*` qui portent un `id` ; sa
forme et son prédicat vivent dans `oto_mcp/db/index_releve.py` (`GESTES_DE_LIGNE`),
partagés avec le démarrage d'une base neuve. Le prédicat est impliqué par la requête :
mêmes `tool LIKE 'data\\_%'`, et l'égalité sur `ns_id` et sur `id`.

**Posé CONCURRENTLY**, sous le verdict commun (`db/index_concurrent.py`) : la révision ne
construit que sur une `tool_calls` PETITE (base neuve, de test) ; sur la base servie
(~12,7 M lignes), elle lève `ConstructionManuelleRequise`, et le geste est manuel
(docs/migrations-versionnees.md §5.1) : `oto-mcp maintenance index-concurrents 0054`,
puis `oto-mcp migrer upgrade head`. Rejouée après le geste, elle constate l'index valide
et ne fait rien. Un index INVALIDE lève `IndexInvalide`, jamais pris pour fait.

**Verrous.** `ShareUpdateExclusiveLock` sur `tool_calls` seulement : ni lectures ni
écritures bloquées. `lock_timeout` 5 min.

**Ordre avec le code : indifférent.** Le code qui lit par (tableau, ligne) répond juste
sans l'index, plus lentement. Prod et préprod partagent la base : posé une fois, il sert
les deux.

Retour arrière : `DROP INDEX CONCURRENTLY IF EXISTS idx_tool_calls_ns_row`.

Révision : 0054_tool_calls_gestes_de_ligne
Précédente : 0053_connector_acl_retiree
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db import index_concurrent, index_releve

revision = "0054_tool_calls_gestes_de_ligne"
down_revision = "0053_connector_acl_retiree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_concurrent.poser_par_revision(op, index_releve.GESTES_DE_LIGNE)


def downgrade() -> None:
    index_concurrent.retirer_par_revision(op, index_releve.GESTES_DE_LIGNE)
