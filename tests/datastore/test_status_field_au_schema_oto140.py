"""La colonne d'état est celle qui porte le `lifecycle` — rien d'autre à déclarer.

Trois mécanismes ont désigné cette colonne en une journée :

1. `role: "status"` — une étiquette, qui se pose autant de fois qu'on veut ; le
   PREMIER champ trouvé gagnait, donc l'ordre de déclaration tranchait en silence ;
2. `status_field: "statut"` au niveau du schéma — mieux (une colonne absente se
   refuse), mais **deux façons de dire un seul fait**, ce qui est le doublon qu'on
   voulait supprimer ;
3. **le bloc `lifecycle` désigne sa colonne** — il est déjà posé dessus.

⚠️ **Le troisième est le seul qui n'ait rien à synchroniser.** Et il fait disparaître
un défaut au lieu de le garder : avant, un `lifecycle` posé sur une colonne non
étiquetée était stocké, servi… et jamais lu. **Cinq tableaux étaient dans ce cas, dont
quatre en production**, et leurs auteurs croyaient avoir armé une file de travail — ni
état terminal, ni plafond de reprises, ni périmètre de réservation.

Ce défaut n'existe plus **par construction**, pas grâce à une garde. C'est la
différence entre empêcher une faute et la signaler.
"""
from __future__ import annotations

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.declaration import status_field
from oto_mcp.datastore.definition import validate_schema_def

_LC = {"states": ["a", "b"], "terminal": ["b"]}


def _schema(*porteurs, **extra):
    champs = [{"key": "siren", "type": "text"},
              {"key": "statut", "type": "text"},
              {"key": "suivi", "type": "text"}]
    for f in champs:
        if f["key"] in porteurs:
            f["lifecycle"] = dict(_LC)
    return {"key": "siren", "fields": champs, **extra}


# ── ce que le bloc désigne ───────────────────────────────────────────────────

def test_la_colonne_qui_porte_le_bloc_EST_l_etat():
    assert status_field(_schema("statut"))["key"] == "statut"
    assert status_field(_schema("suivi"))["key"] == "suivi"


def test_sans_bloc_il_n_y_a_PAS_d_etat():
    """Un tableau sans cycle de vie n'a pas de colonne d'état — et c'est le cas
    normal : la plupart des tableaux n'en ont pas."""
    assert status_field(_schema()) is None


def test_l_ancienne_ETIQUETTE_ne_designe_plus_rien():
    """⚠️ `role: "status"` n'est plus lu. Le porter sans bloc ne fait pas de cette
    colonne un état — sinon on aurait gardé deux façons de dire la même chose."""
    sch = _schema()
    sch["fields"][1]["role"] = "status"

    assert status_field(sch) is None


def test_le_bloc_gagne_meme_si_l_etiquette_designe_une_AUTRE_colonne():
    """Le cas exact des cinq tableaux : l'étiquette sur `statut`, le bloc sur `suivi`.
    Avant, le bloc de `suivi` n'était jamais lu et personne ne le disait."""
    sch = _schema("suivi")
    sch["fields"][1]["role"] = "status"

    assert status_field(sch)["key"] == "suivi"


# ── ce que la pose refuse ────────────────────────────────────────────────────

def test_DEUX_cycles_de_vie_sont_refuses_a_la_pose():
    """⚠️ Sinon le premier trouvé gagnerait, et l'ordre de déclaration trancherait en
    silence — exactement ce que le retrait de l'étiquette supprime. Même refus que deux
    colonnes `display: "title"`."""
    erreurs = validate_schema_def(_schema("statut", "suivi"))

    assert erreurs
    assert "une seule porte le cycle de vie" in erreurs[0]
    assert "statut" in erreurs[0] and "suivi" in erreurs[0], "le refus NOMME les deux"
    assert "stocké, servi, et jamais lu" in erreurs[0], "et dit ce qu'on éviterait"


def test_UN_seul_bloc_est_valide():
    assert validate_schema_def(_schema("statut")) == []


def test_AUCUN_bloc_est_valide():
    """Le cas de loin le plus fréquent dans le parc."""
    assert validate_schema_def(_schema()) == []


# ── ce que le cycle de vie lit ───────────────────────────────────────────────

def test_le_cycle_se_lit_sur_la_colonne_qui_le_porte():
    """La boucle est fermée : `lifecycle_of` passe par `status_field`, qui rend la
    colonne portant le bloc. Plus rien à faire correspondre."""
    assert dsv2.lifecycle_of(_schema("suivi")) == _LC
    assert dsv2.terminal_states(_schema("suivi")) == {"b"}


def test_l_avertissement_du_cycle_HORS_statut_n_a_plus_d_objet():
    """⚠️ Il existait pour dire « ce bloc n'est pas lu ». Un bloc non lu ne peut plus
    exister : soit il est seul et il EST l'état, soit il y en a deux et la pose refuse.
    L'avertissement se tait donc, et c'est le signe que le défaut est fermé."""
    assert dsv2.lifecycle_hors_statut(_schema("suivi")) == []
    assert dsv2.lifecycle_hors_statut(_schema("statut")) == []
