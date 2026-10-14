"""Les quatre contraintes du CREATE TABLE qu'une base servie n'avait jamais reçues (#786).

Une contrainte déclarée inline dans un `CREATE TABLE IF NOT EXISTS` n'arrive que sur une
base neuve. Constat du 14/10/2026 (lecture seule) sur deux bases qui servent le même code :
l'instance née après ces contraintes les porte, l'instance principale ne les a pas (elle
n'a que les index uniques du démarrage, et aucun CHECK sur `role`). Zéro violation dans les
deux. Cette révision pose, sous les NOMS du `CREATE TABLE`
(`db/contraintes_create_table.py`) :

- `projects_mcp_slug_key` UNIQUE (mcp_slug) ;
- `org_instructions_owner_pkey` PRIMARY KEY (owner_type, owner_id, slug) ;
- `org_instruction_revisions_owner_pkey` PRIMARY KEY (owner_type, owner_id, slug, version) ;
- `resource_grants_role_check` CHECK (role IN ('viewer', 'editor', 'manager')).

**Ce qu'elle fait**, contrainte par contrainte, après un constat au catalogue
(`pg_constraint`, `pg_attribute`, `pg_index`) qui ne prend aucun verrou de table, sauf
l'`AccessShareLock` d'un SELECT sur `resource_grants` (relire l'expression du CHECK) :

- déjà posée sous la forme attendue → rien (l'instance qui les porte : aucun ordre) ;
- posée sous le même nom avec une autre forme → `ContrainteDivergente`, rien n'est touché ;
- clé : l'index de la contrainte, sous son nom, construit CONCURRENTLY hors transaction
  (`index_concurrent.poser_par_revision` : absent → construit, invalide → `IndexInvalide`,
  table > 100 000 lignes → `ConstructionManuelleRequise`, geste
  `oto-mcp maintenance index-concurrents 0057`), sa forme vérifiée, puis
  `ADD CONSTRAINT … USING INDEX` (catalogue seul). Les index `uq_…` du démarrage restent :
  une base neuve les porte aussi, et les consommer ferait reconstruire `uq_…` au démarrage
  suivant, non concurrent. Une clé primaire sur une colonne nullable lève
  (`ColonnesNullables`) au lieu de parcourir la table sous verrou exclusif ;
- CHECK : `NOT VALID` (catalogue seul), puis `VALIDATE CONSTRAINT` dans une AUTRE
  transaction. Reprise après interruption : une contrainte `NOT VALID` est validée.

Chaque ordre part dans sa propre transaction (autocommit) : aucun verrou exclusif n'est
tenu au-delà de son ordre.

**Verrous** (sur l'instance principale seulement) :
- construction de l'index (×3) : `ShareUpdateExclusiveLock`, ni lectures ni écritures
  bloquées, attente bornée à 5 min ; tables de quelques milliers de lignes : < 1 s ;
- promotion (×3) et pose `NOT VALID` (×1) : `AccessExclusiveLock` bref (écriture de
  catalogue, aucun parcours), attente bornée à 5 s — échue, la révision lève et se rejoue ;
- validation du CHECK : `ShareUpdateExclusiveLock` sur `resource_grants`, un parcours de
  la table, lectures et écritures libres, attente bornée à 5 min.

**Ordre avec le code : indifférent.** Aucun code ne dépend de ces contraintes, et le code
courant comme l'ancien tournent déjà contre l'instance qui les porte. Prod et préprod
partagent la base : jouée une fois, elle sert les deux.

**Retour arrière : `alembic stamp 0056_limite_refus_jour`**, sans toucher aux
contraintes — aucune révision antérieure ni aucun code n'en souffre. `downgrade` lève :
retirer ces contraintes sur une base née avec elles l'éloignerait de son `CREATE TABLE`.
Retrait manuel d'une seule, si elle gênait : `ALTER TABLE <table> DROP CONSTRAINT <nom>`
(emporte l'index de la clé ; les `uq_…` restent).

Révision : 0057_contraintes_create_table
Précédente : 0056_limite_refus_jour
"""
from __future__ import annotations

from alembic import context, op

from oto_mcp.db import index_concurrent
from oto_mcp.db.contraintes_create_table import (
    ATTENTE_BREVE,
    CLES,
    REVISION,
    VERIFICATIONS,
    cle_posee,
    etat_verification,
    verifier_index,
)

revision = REVISION
down_revision = "0056_limite_refus_jour"
branch_labels = None
depends_on = None


def _hors_transaction(ordre: str, attente: str) -> None:
    """Un ordre seul, dans sa propre transaction, l'attente de verrou bornée."""
    with op.get_context().autocommit_block():
        op.execute(attente)
        try:
            op.execute(ordre)
        finally:
            op.execute(index_concurrent.ATTENTE_RENDUE)


def _scalaire(sql: str):
    return op.get_bind().exec_driver_sql(sql).scalar()


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc n'a pas de base à lire : les ordres tels qu'ils partiraient sur
        # la base qui n'a aucune de ces contraintes.
        for cle in CLES:
            with op.get_context().autocommit_block():
                op.execute(cle.index.ddl_concurrent)
            op.execute(cle.ddl_promotion)
        for verif in VERIFICATIONS:
            op.execute(verif.ddl_pose)
            op.execute(verif.ddl_validation)
        return
    for cle in CLES:
        if cle_posee(cle, _scalaire):
            continue
        index_concurrent.poser_par_revision(op, cle.index)
        verifier_index(cle, _scalaire)
        _hors_transaction(cle.ddl_promotion, ATTENTE_BREVE)
    for verif in VERIFICATIONS:
        etat = etat_verification(verif, _scalaire)
        if etat == "absente":
            _hors_transaction(verif.ddl_pose, ATTENTE_BREVE)
        if etat != "posee":
            _hors_transaction(verif.ddl_validation, index_concurrent.ATTENTE_MAX)


def downgrade() -> None:
    raise RuntimeError(
        "0057_contraintes_create_table ne se défait pas : ces contraintes sont celles du "
        "CREATE TABLE, et une base née avec elles les porterait encore. Pour descendre "
        "sous 0057, `alembic stamp 0056_limite_refus_jour` (aucune révision antérieure ni "
        "aucun code n'en dépend).")
