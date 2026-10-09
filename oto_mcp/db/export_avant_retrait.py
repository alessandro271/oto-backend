"""Une table ne part qu'exportée : l'attestation que pose l'export et qu'exige le retrait.

Né pour le retrait d'une table morte qui porte encore des données (oto-backend#1169) :
la révision qui la supprime ne doit jamais partir avant que son contenu ait été
exporté, relu, et que rien n'ait bougé depuis.

**L'inscription est un commentaire de la table elle-même** (`COMMENT ON TABLE`), posé
par le script d'export APRÈS la relecture de l'objet déposé :

    oto:exportee {"objet": "s3://…/….csv.gz", "lignes": 55, "empreinte": "<md5>", "le": "…"}

Pourquoi là plutôt qu'une ligne d'un registre : c'est le mécanisme le plus simple qui
empêche un `DROP` sans export. Aucune table ni colonne neuve, rien à créer avant
l'export (qui précède le déploiement), et l'attestation vit et meurt avec l'objet
qu'elle atteste — une table recréée vide n'en hérite pas. Écrire un commentaire exige
d'être propriétaire de la table, comme la supprimer : qui peut l'un peut l'autre.

**Ce que le retrait vérifie** (`exiger`), sous le verrou qui précède le `DROP` :
- table VIDE → rien à perdre, aucune attestation exigée (base neuve, instance cible où
  l'import ne l'a pas versée) ;
- sinon, une attestation doit exister, et son compte ET son empreinte doivent être ceux
  de la table à cet instant. Une ligne ajoutée, retirée ou modifiée depuis l'export lève
  `ExportNonAtteste` : on réexporte, on ne supprime pas.

**L'empreinte** est le md5 des lignes en JSON (`to_jsonb`), triées : indépendante de
l'ordre physique. Un `timestamptz` s'y écrit dans le fuseau de la session : il est fixé
à UTC le temps du calcul (`set_config(…, true)`, local à la transaction), puis rendu.

Les fonctions prennent `scalaire(sql) -> valeur` : la révision passe par SQLAlchemy,
le script par psycopg — une seule logique pour les deux, comme `index_concurrent`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

MARQUE = "oto:exportee "

_NOM = re.compile(r"[a-z_][a-z0-9_]*")
_FUSEAU = re.compile(r"[A-Za-z0-9_+\-/:.]+")

Scalaire = Callable[[str], Any]


class ExportNonAtteste(RuntimeError):
    """La table porte des lignes qu'aucun export attesté ne couvre : pas de retrait."""


def _nom(table: str) -> str:
    if not _NOM.fullmatch(table):
        raise ValueError(f"nom de table invalide : {table!r}")
    return table


def releve(scalaire: Scalaire, table: str) -> tuple[int, str]:
    """(lignes, empreinte) de la table, lus dans la transaction de l'appelant."""
    t = _nom(table)
    fuseau = scalaire("SELECT current_setting('TimeZone')")
    if not _FUSEAU.fullmatch(fuseau or ""):
        raise ValueError(f"fuseau de session inattendu : {fuseau!r}")
    scalaire("SELECT set_config('TimeZone', 'UTC', true)")
    try:
        brut = scalaire(
            f"SELECT count(*)::text || ':' || md5(coalesce(string_agg(x, E'\\n' ORDER BY x), '')) "
            f"FROM (SELECT to_jsonb(t)::text AS x FROM {t} t) s")
    finally:
        scalaire(f"SELECT set_config('TimeZone', '{fuseau}', true)")
    lignes, empreinte = brut.split(":", 1)
    return int(lignes), empreinte


def texte(objet: str, lignes: int, empreinte: str, le: str) -> str:
    """Le commentaire que pose l'export."""
    return MARQUE + json.dumps({"objet": objet, "lignes": lignes, "empreinte": empreinte,
                                "le": le}, ensure_ascii=False, sort_keys=True)


def lire(scalaire: Scalaire, table: str) -> Optional[dict]:
    """L'attestation posée sur la table, ou None (pas de commentaire, ou pas le nôtre)."""
    commentaire = scalaire(f"SELECT obj_description(to_regclass('{_nom(table)}'), 'pg_class')")
    if not commentaire or not commentaire.startswith(MARQUE):
        return None
    return json.loads(commentaire[len(MARQUE):])


def exiger(scalaire: Scalaire, table: str, commande: str) -> int:
    """Lève `ExportNonAtteste` si la table porte des lignes qu'aucun export attesté ne
    couvre exactement ; rend le nombre de lignes sinon. `commande` est le geste qui
    exporte, cité dans le refus. À appeler SOUS le verrou qui précède le retrait."""
    lignes, empreinte = releve(scalaire, table)
    if lignes == 0:
        return 0
    atteste = lire(scalaire, table)
    if atteste is None:
        raise ExportNonAtteste(
            f"`{table}` porte {lignes} ligne(s) et aucun export attesté : rien n'est "
            f"supprimé. Exporter d'abord ({commande}), puis rejouer la révision.")
    if (atteste.get("lignes"), atteste.get("empreinte")) != (lignes, empreinte):
        raise ExportNonAtteste(
            f"`{table}` a changé depuis son export ({atteste.get('objet')}, "
            f"{atteste.get('lignes')} ligne(s) attestée(s), {lignes} aujourd'hui, empreinte "
            f"{'identique' if atteste.get('empreinte') == empreinte else 'différente'}) : "
            f"rien n'est supprimé. Réexporter ({commande}), puis rejouer la révision.")
    return lignes
