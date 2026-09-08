"""`status_field` — la colonne d'état se déclare au SCHÉMA, comme la clé métier.

Jusqu'ici, la colonne portant le cycle de vie se désignait par une étiquette sur le
champ (`role: "status"`), et `status_field()` rendait **le premier champ trouvé**.

⚠️ **Une étiquette se pose autant de fois qu'on veut.** Deux champs marqués, et c'est
l'ORDRE DE DÉCLARATION qui tranche — en silence, sans qu'aucun texte le dise. Une clé
au niveau du schéma ne peut désigner qu'une colonne, et une colonne inexistante se
refuse à la pose.

C'est la leçon déjà tirée deux fois dans ce dépôt : `key` pour la clé métier, et
`display: "title"` qui a remplacé `role: "title"` pour cette raison exacte.

**Ce qui l'a déclenché**, mesuré le 08/09/2026 sur quatre tableaux d'une campagne :
un `lifecycle` posé sur une colonne SANS le rôle — donc jamais lu, jamais appliqué,
et le schéma affichait le contraire. La garde qu'ils croyaient armer ne gardait rien.

Palier 1 : la clé s'ajoute et gagne quand elle est là. `role: "status"` continue de
valoir — rien ne casse, et son retrait viendra avec son préavis.
"""
from __future__ import annotations

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.declaration import STATUS_KEY, status_field
from oto_mcp.datastore.definition import validate_schema_def


def _schema(**extra):
    return {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "statut", "type": "text"},
        {"key": "suivi", "type": "text"},
    ], **extra}


# ── ce que la clé change ─────────────────────────────────────────────────────

def test_la_cle_de_schema_designe_la_colonne():
    assert status_field(_schema(status_field="suivi"))["key"] == "suivi"


def test_elle_GAGNE_sur_l_etiquette():
    """Palier 1 : les deux formes coexistent, la clé explicite décide. Sinon la
    bascule dépendrait de l'ordre dans lequel un tableau est migré."""
    sch = _schema(status_field="suivi")
    sch["fields"][1]["role"] = "status"          # `statut` porte l'ancienne étiquette

    assert status_field(sch)["key"] == "suivi"


def test_sans_la_cle_l_etiquette_vaut_TOUJOURS():
    """⚠️ La moitié qui garantit que rien ne casse : les tableaux de production
    n'ont pas la clé, et leur cycle de vie doit continuer de s'appliquer."""
    sch = _schema()
    sch["fields"][1]["role"] = "status"

    assert status_field(sch)["key"] == "statut"


def test_aucune_des_deux_rend_None():
    assert status_field(_schema()) is None


# ── ce que le refus attrape ──────────────────────────────────────────────────

def test_nommer_une_colonne_ABSENTE_est_refuse_a_la_POSE():
    """⚠️ Le cœur du gain. Une étiquette mal posée ne se voit jamais ; une clé qui
    nomme une colonne inexistante se refuse au moment où on peut encore corriger.
    Sans ce refus, le cycle de vie ne s'appliquerait à rien — le défaut exact qu'on
    vient de mesurer sur un tableau de campagne."""
    erreurs = validate_schema_def(_schema(status_field="inexistante"))

    assert erreurs
    assert "ne désigne aucune colonne" in erreurs[0]
    assert "aucun état terminal" in erreurs[0], "le refus dit ce qu'on perd"


def test_une_valeur_qui_n_est_pas_un_NOM_est_refusee():
    erreurs = validate_schema_def(_schema(status_field={"key": "statut"}))

    assert erreurs and "doit être le NOM d'une colonne" in erreurs[0]


def test_un_schema_SANS_la_cle_reste_valide():
    """Le cas de tous les tableaux existants : la clé est facultative."""
    assert validate_schema_def(_schema()) == []


# ── ce que la lecture voit ───────────────────────────────────────────────────

def test_l_avertissement_du_cycle_hors_statut_suit_la_cle():
    """L'avertissement qui dit « ce `lifecycle` n'est pas lu » doit juger sur la MÊME
    colonne que le mécanisme. Deux façons de désigner l'état, et il crierait sur la
    bonne colonne ou se tairait sur la mauvaise."""
    sch = _schema(status_field="suivi")
    sch["fields"][1]["lifecycle"] = {"states": ["a", "b"]}      # sur `statut`
    sch["fields"][2]["lifecycle"] = {"states": ["x", "y"]}      # sur `suivi`

    hors = dsv2.lifecycle_hors_statut(sch)
    assert hors == ["statut"], "c'est `suivi` qui porte l'état, donc `statut` est inerte"
