"""Le texte des VALEURS SERVIES d'une ligne, MATÉRIALISÉ — la portée `values` de `q`.

**Pourquoi (16/10/2026).** La recherche `q_scope=values` passait par deux GIN
d'EXPRESSION sur `datastore_valeurs_texte_v1(data)` (`search.INDEX_VALEURS`). Trouver
les candidats ne coûte rien (< 1 ms), mais l'index trigramme exige de REVÉRIFIER chaque
candidat, ce qui recalcule la fonction ligne à ligne ; le classement (`ts_rank_cd`) la
recalcule une deuxième fois, le `COUNT` du total une troisième. Le coût suivait donc le
nombre de lignes TROUVÉES, pas `limit` : 19 à 22 s en prod pour un mot présent dans
1 243 lignes d'un tableau d'environ 260 colonnes, avec `limit=8`. Stocké par ligne, le
texte se relit au lieu de se recalculer : revérification et classement lisent une
colonne.

**Deux colonnes, nullables, sans défaut** (ajout instantané, aucune réécriture — la
forme de `search_vec`, #318, et pour la même raison : `GENERATED … STORED` réécrit la
table sous verrou exclusif) :
- `valeurs_texte` : le texte replié des accents (`_fold`), lu par le trigramme ;
- `valeurs_vec` : son `to_tsvector('french', …)`, lu par la moitié lexicale ET par le
  classement.

**Remplies par un DÉCLENCHEUR, pas par le code** — même raison que la révision de ligne
(`db/revision.py`) : prod et préprod partagent la base, la bascule bleu/vert sert deux
versions pendant 120 s, et l'ANCIEN code, qui ignore ces colonnes, écrit aussi. Seul
PostgreSQL voit toutes les écritures : insertion, `UPDATE` de `data` sous toutes ses
formes (`update_where`, `jsonb_set`, import, renommage ou retrait de colonne qui
réécrit `data`), `COPY`. Le texte ne dépend QUE de `data` — ni du schéma du tableau, ni
de rien d'autre : un changement de schéma qui ne réécrit pas `data` ne change aucun
texte servi, et celui qui le réécrit passe par le déclencheur.

`BEFORE … OF data` calcule sur `NEW.data` : jamais le piège du `SET data = <neuf>, vec =
f(data)` qui calculerait sur l'ancienne valeur (cf. `search.stamp_rank_vector`). Une
écriture qui laisse `data` intact sur une ligne déjà remplie ne recalcule rien ; sur une
ligne VIDE, elle la remplit — c'est le geste du rattrapage (`SET data = data`, qui
n'avance ni la révision ni le journal, tous deux conditionnés à un `data` différent).

**Les lignes nées avant** restent vides jusqu'au rattrapage
(`scripts/remplir_valeurs_servies.py`). Parti pris : **jamais perdues**. Tant qu'une
ligne est vide, la requête la cherche par l'ancien prédicat (le GIN d'expression,
inchangé) ; remplie, par ses colonnes. Pas d'instant de bascule à ne pas rater.

**Lues seulement quand leurs index sont valides** (`pretes`). Avant — base servie dont
les index attendent leur construction manuelle —, la requête garde exactement l'ancien
chemin : un prédicat sur des colonnes sans index ferait parcourir tout le tableau.
"""
from __future__ import annotations

import logging
import time

from ._conn import _connect
from .index_concurrent import IndexConcurrent, poser_au_demarrage, scalaire_de
from .paths import FONCTION_VALEURS_TEXTE
from .projects import _fold

logger = logging.getLogger(__name__)

TABLE = "datastore_rows"
COLONNE_TEXTE = "valeurs_texte"
COLONNE_VECTEUR = "valeurs_vec"
REVISION = "0061_valeurs_materialisees"


def texte_de(data: str) -> str:
    """Le texte cherché d'une expression `data` : les valeurs servies, repliées. LA
    source unique — le déclencheur et le repli des lignes vides le lisent tous deux."""
    return _fold(f"{FONCTION_VALEURS_TEXTE}({data})")


DDL_COLONNES = {
    COLONNE_TEXTE: f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLONNE_TEXTE} text",
    COLONNE_VECTEUR: f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLONNE_VECTEUR} tsvector",
}

NOM_FONCTION = "datastore_valeurs_materialisees"
#: Après `datastore_rows_20_revision` (les `BEFORE ROW` s'exécutent dans l'ordre
#: alphabétique de leur nom) : l'ordre est indifférent ici, le préfixe le rend lisible.
NOM_DECLENCHEUR = "datastore_rows_25_valeurs"

DDL_FONCTION = (
    f"CREATE OR REPLACE FUNCTION {NOM_FONCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ "
    "BEGIN "
    "IF TG_OP = 'UPDATE' AND NEW.data IS NOT DISTINCT FROM OLD.data "
    f"AND NEW.{COLONNE_TEXTE} IS NOT NULL THEN RETURN NEW; END IF; "
    f"NEW.{COLONNE_TEXTE} := {texte_de('NEW.data')}; "
    f"NEW.{COLONNE_VECTEUR} := to_tsvector('french', NEW.{COLONNE_TEXTE}); "
    "RETURN NEW; END $$")

DDL_DECLENCHEUR = (
    f"CREATE TRIGGER {NOM_DECLENCHEUR} BEFORE INSERT OR UPDATE OF data ON {TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {NOM_FONCTION}()")

SQL_DECLENCHEUR_PRESENT = (
    f"SELECT 1 AS ok FROM pg_trigger WHERE tgrelid = '{TABLE}'::regclass "
    f"AND tgname = '{NOM_DECLENCHEUR}'")

#: Même seuil que les GIN d'expression qu'ils relaient : au-delà, la construction se
#: fait à la main, CONCURRENTLY (docs/migrations-versionnees.md §5.1).
INDEX_VECTEUR = IndexConcurrent(nom="idx_datastore_rows_valeurs_vec", table=TABLE,
                                forme=f"USING GIN ({COLONNE_VECTEUR})",
                                revision=REVISION, max_lignes=10_000)
INDEX_TRIGRAMME = IndexConcurrent(nom="idx_datastore_rows_valeurs_texte_trgm", table=TABLE,
                                  forme=f"USING GIN ({COLONNE_TEXTE} gin_trgm_ops)",
                                  revision=REVISION, max_lignes=10_000)
INDEX = (INDEX_VECTEUR, INDEX_TRIGRAMME)

SQL_RESTANT = f"SELECT count(*) AS n FROM {TABLE} WHERE {COLONNE_TEXTE} IS NULL"


SQL_COLONNE_PRESENTE = ("SELECT 1 FROM information_schema.columns "
                        f"WHERE table_name = '{TABLE}' AND column_name = '{{}}'")

#: Le retour arrière d'une révision : tout, dans l'ordre inverse de la pose (les index
#: d'abord, par `index_concurrent.retirer_par_revision`).
DDL_RETRAIT = (f"DROP TRIGGER IF EXISTS {NOM_DECLENCHEUR} ON {TABLE}",
               f"DROP FUNCTION IF EXISTS {NOM_FONCTION}()",
               f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {COLONNE_VECTEUR}",
               f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {COLONNE_TEXTE}")


def ordres_a_jouer(scalaire) -> list[str]:
    """Le DDL qui MANQUE (colonnes, déclencheur), et la fonction, toujours reposée —
    lu par le démarrage comme par la révision 0061, un seul verdict.

    ⚠️ `ADD COLUMN IF NOT EXISTS` prend son `AccessExclusiveLock` AVANT de constater
    qu'il n'a rien à faire, et `CREATE TRIGGER` un `SHARE ROW EXCLUSIVE` : sur
    `datastore_rows`, à chaque démarrage, ces verrous pris pour rien bloqueraient la
    production (cf. `db/revision.py`). Le catalogue d'abord. `CREATE OR REPLACE
    FUNCTION` ne verrouille pas la table : reposée à chaque fois, elle suit le code.
    Ordre : colonnes, fonction (qui les écrit), déclencheur (qui l'appelle)."""
    ordres = [ddl for colonne, ddl in DDL_COLONNES.items()
              if scalaire(SQL_COLONNE_PRESENTE.format(colonne)) is None]
    ordres.append(DDL_FONCTION)
    if scalaire(SQL_DECLENCHEUR_PRESENT) is None:
        ordres.append(DDL_DECLENCHEUR)
    return ordres


def poser(conn) -> list[str]:
    """Colonnes, fonction, déclencheur, index — sur la connexion DDL du démarrage.
    Les index suivent le verdict CONCURRENTLY (`index_concurrent`) : construits ici sur
    une base neuve seulement ; une base servie les reçoit de la révision 0061 ou à la
    main (`oto-mcp maintenance index-concurrents 0061`). Rend les ordres joués."""
    ordres = ordres_a_jouer(scalaire_de(conn))
    for ordre in ordres:
        conn.execute(ordre)
    for index in INDEX:
        poser_au_demarrage(conn, index)
    return ordres


# ── Les colonnes sont-elles LISIBLES ? ───────────────────────────────────────────

#: Relu au plus toutes les 60 s par processus : la construction manuelle des index se
#: voit dans la minute, sans une requête au catalogue par recherche.
DUREE_CACHE_S = 60.0
_cache: dict = {"pretes": None, "lu_a": 0.0}

SQL_INDEX_VALIDES = (
    "SELECT count(*) FILTER (WHERE i.indisvalid) = %s AS ok FROM pg_index i "
    "WHERE i.indexrelid IN (" + ", ".join(
        f"to_regclass('{index.nom}')" for index in INDEX) + ")")


def pretes() -> bool:
    """`True` quand les deux index des colonnes sont posés ET valides — la requête lit
    alors les colonnes. Sinon, l'ancien chemin, à l'identique."""
    maintenant = time.monotonic()
    if _cache["pretes"] is not None and maintenant - _cache["lu_a"] < DUREE_CACHE_S:
        return _cache["pretes"]
    with _connect() as conn:
        ligne = conn.execute(SQL_INDEX_VALIDES, (len(INDEX),)).fetchone()
    _cache.update(pretes=bool(ligne and ligne["ok"]), lu_a=maintenant)
    return _cache["pretes"]


def lit_les_colonnes(recherche) -> bool:
    """La recherche `recherche` lit-elle les colonnes ? Portée `values`, index prêts.
    La portée `all` a son propre vecteur matérialisé (`search_vec`, #318)."""
    return recherche.portee == "values" and pretes()


def oublier() -> None:
    """Relit le catalogue au prochain appel (bancs, et après un geste manuel)."""
    _cache.update(pretes=None, lu_a=0.0)
