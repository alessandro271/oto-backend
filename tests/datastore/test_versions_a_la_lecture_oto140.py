"""`versions` — quelles versions d'une case la lecture rend (oto#140).

Une case existe en deux versions : `current`, ce qu'on a établi, et `origine`, ce que
la cliente a remis. Une écriture vise toujours la courante ; une lecture doit pouvoir
dire ce qu'elle veut recevoir.

Palier 1 : l'option existe, **le défaut ne bouge pas**. La bascule vers `current` seul
viendra avec un préavis daté et 24 h d'annonce au consommateur qui lit la version de
départ — son écran des écarts et celui de complétude en dépendent, c'est toute leur
raison d'être.

⚠️ Cette bascule n'est pas une économie de données : elle **supprime la surface d'un
incident**. Un écran avait comparé la valeur de départ à la valeur courante et
s'apprêtait à annoncer à une cliente « nous avons corrigé votre valeur » en lui
montrant du texte écrit par la plateforme. Ce genre d'accident demande que la donnée
soit là sans qu'on l'ait demandée.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore import versions as dsver
# `live` vient de `conftest.py` — pytest la découvre, rien à importer.

def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})



def _table():
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "raison_sociale", "type": "text"}]})
    st.append_row(ns, {"siren": "1", "raison_sociale":
                       {"valeur": "DUPONT", "comment": "fichier cliente 05/08"}},
                  donnees_d_origine=True)
    st.update_row(ns, st.list_rows(ns)[0]["_id"],
                  {"raison_sociale": {"valeur": "Dupont SAS", "comment": "INSEE"}})
    return st, ns


# ── le paramètre : ce qu'il admet et ce qu'il refuse ──────────────────────────

def test_le_defaut_ne_bouge_PAS_encore():
    """Palier 1. Un défaut qui basculerait dans le même lot ferait changer les écrans
    deux fois : une fois pour nommer, une fois pour ne plus recevoir."""
    assert dsver.check(None) == (dsver.CURRENT, dsver.ORIGINE)


def test_l_ordre_demande_ne_change_PAS_ce_qui_est_declare():
    """Deux appelants qui demandent la même chose dans un ordre différent doivent lire
    la même déclaration — sinon `versions_servies` devient incomparable."""
    assert dsver.check(["origine", "current"]) == dsver.check(["current", "origine"])


def test_une_liste_VIDE_est_refusee_et_non_traitee_comme_un_defaut():
    """⚠️ `versions=[]` est un geste délibéré qui ne veut rien dire. Le lire comme un
    défaut servirait exactement l'INVERSE de ce qu'il demande, et sans un mot."""
    with pytest.raises(ValueError) as e:
        dsver.check([])
    assert "non vide" in str(e.value)


def test_le_refus_NOMME_ce_qui_est_admis():
    """Un `invalid_input` nu oblige à deviner. Ici le refus rend les deux versions et
    ce que chacune veut dire — y compris pour qui essaierait l'ancien mot français."""
    with pytest.raises(ValueError) as e:
        dsver.check(["actuel"])
    msg = str(e.value)
    assert "actuel" in msg and "current" in msg and "origine" in msg


# ── ce que la lecture sert vraiment ───────────────────────────────────────────

def test_sans_origine_demandee_la_couche_DISPARAIT(live):
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.CURRENT,))[0]

    assert ligne["raison_sociale"] == "Dupont SAS"
    assert not [k for k in ligne if k.startswith("raison_sociale.origine")]


def test_avec_origine_demandee_la_couche_ET_ses_sous_champs_reviennent(live):
    """Le cas de l'écran d'écart : la valeur de départ ET sa provenance, pour dire
    « vous nous aviez donné X, source Y » — ce qu'on ne pouvait pas dire avant."""
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.CURRENT, dsver.ORIGINE))[0]

    assert ligne["raison_sociale"] == "Dupont SAS"
    assert ligne["raison_sociale.origine"] == "DUPONT"
    assert ligne["raison_sociale.origine.comment"] == "fichier cliente 05/08"


def test_le_NOM_NU_porte_toujours_la_version_COURANTE(live):
    """⚠️ L'axe qui compte le plus, et le piège qu'on refuse d'installer : faire porter
    deux sens à `champ` selon un paramètre serait exactement ce qu'on retire ailleurs
    du produit — un mot, deux choses. Même en ne demandant QUE l'origine, `champ` reste
    la valeur courante ; `versions` décide seulement de ce qui S'AJOUTE."""
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.ORIGINE,))[0]

    assert ligne["raison_sociale"] == "Dupont SAS", (
        "`champ` doit rester la version courante, quelle que soit la demande")
    assert ligne["raison_sociale.origine"] == "DUPONT"


def test_les_deux_versions_arrivent_dans_le_MEME_appel(live):
    """⚠️ Demandé par le consommateur avec un argument qui tranche : deux appels ne
    sont pas ATOMIQUES. Une écriture entre les deux ferait comparer l'avant d'un état
    à l'après d'un autre, et l'écran dirait « corrigé » sur une ligne que personne n'a
    touchée — la famille d'accident que ce contrat existe pour fermer."""
    st, ns = _table()
    ligne = st.list_rows(ns, versions=(dsver.CURRENT, dsver.ORIGINE))[0]

    assert ligne["raison_sociale"] != ligne["raison_sociale.origine"], (
        "l'écart doit être lisible sur la MÊME ligne, sans jointure ni second appel")


# ── ce que la réponse DÉCLARE ─────────────────────────────────────────────────

def test_la_reponse_declare_ce_qu_elle_sert(live):
    """Sans elle, « je ne l'ai pas demandée » et « elle n'existe pas sur cette case »
    se lisent pareil — et le lecteur réinventerait un marqueur, en pire, puisque cette
    fois il l'aurait deviné."""
    st, ns = _table()

    page = st.cursor_rows(ns, versions=(dsver.CURRENT,))
    assert page["versions_servies"] == ["current"]

    page = st.cursor_rows(ns, versions=(dsver.CURRENT, dsver.ORIGINE))
    assert page["versions_servies"] == ["current", "origine"]


def test_la_page_REST_la_declare_aussi(live):
    """Les deux faces lisent le même stockage : une déclaration servie d'un seul côté
    ferait diverger ce que chacune promet."""
    st, ns = _table()
    assert st.page_rows(ns, versions=(dsver.ORIGINE,))["versions_servies"] == ["origine"]


def test_le_defaut_se_lit_a_UN_seul_endroit():
    """Même promesse que `layers.DEFAUT`, et pour la même raison : un défaut recopié
    diverge le jour où on le change — et il divergerait sur la surface qu'un agent
    répète le plus."""
    import inspect

    from oto_mcp.tools import datastore as face_mcp
    src = inspect.getsource(face_mcp)
    assert "dsver.check(versions)" in src
    assert '"current", "origine"]' not in src.replace(
        '`versions=["current","origine"]`', "")


def test_les_DEUX_faces_portent_le_parametre():
    """⚠️ Une face branchée et l'autre non fait diverger ce que chacune promet — et
    c'est la face REST qui sert les écrans. Le champ vit dans `_forme.py`, le tiers
    neutre que `rows` et `claim` importent tous les deux : deux définitions
    divergeraient le jour où le défaut bascule."""
    import inspect

    from oto_mcp.capabilities.datastore import _forme, rows as face_rest
    from oto_mcp.tools import datastore as face_mcp

    assert "_VERSIONS" in inspect.getsource(_forme)
    assert "_versions(inp.versions)" in inspect.getsource(face_rest)
    assert "dsver.check(versions)" in inspect.getsource(face_mcp)


def test_le_refus_REST_NOMME_le_parametre():
    """Un `invalid_input` nu obligerait l'appelant à deviner lequel de ses paramètres
    est en cause. Le code d'erreur nomme celui-ci, comme `invalid_layers`."""
    from oto_mcp.capabilities._types import AuthzDenied
    from oto_mcp.capabilities.datastore._forme import _versions

    with pytest.raises(AuthzDenied) as e:
        _versions(["actuel"])
    assert e.value.code == "invalid_versions"
    assert "current" in str(e.value.message if hasattr(e.value, "message") else e.value)
