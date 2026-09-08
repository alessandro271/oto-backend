"""Un effacement qui ne détruit rien, annoncé comme un succès.

`{"contact1_nom": {"link": "@empty"}}` sur une ligne qui porte une relique littérale
`data->>'contact1_nom.link'` était **accepté**, rapporté comme réussi, et **ne touchait
rien** : le geste vise la couche imbriquée, la donnée est ailleurs.

⚠️ Signalé le 08/09/2026 par une campagne qui croyait avoir retiré une adresse de
profil personnel. Elle ne l'a su qu'en relisant par acquit de conscience.

**Un zéro se met en doute ; un succès ne se met pas en doute.** C'est ce qui rend ce
défaut pire que le faux zéro du filtre corrigé le même jour : là on doutait d'un
chiffre, ici on ne doute de rien.

⚠️ **Seul l'EFFACEMENT est refusé, et c'est délibéré.** Refuser toute écriture visant
une couche dont une relique existe casserait le geste normal : 500 lignes d'un tableau
de production portent `qualification.comment` en relique, et leurs agents écrivent
`{"qualification": {"comment": …}}` tous les jours — ce geste-là fonctionne, il écrit
l'imbriqué. Ce qui ne fonctionne pas, c'est de croire DÉTRUIRE.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore import reliques as rq
from oto_mcp.datastore.errors import RowValidationError


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _ligne_avec_relique():
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [{"key": "siren", "type": "text"}]})
    rid = st.append_row(ns, {"siren": "1"})["_id"]
    with _connect() as c:
        c.execute("UPDATE datastore_rows SET data = data || %s::jsonb WHERE row_id=%s",
                  ('{"contact1_nom.link": "https://profil"}', rid))
        c.commit()
    return st, ns, rid


# ── ce que la garde attrape ──────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"contact1_nom": {"link": "@empty"}},   # la forme imbriquée : vise à côté
    {"contact1_nom": {"link": None}},       # `null` : même intention
])
def test_l_effacement_qui_ne_detruirait_rien_est_REFUSE(live, payload):
    st, ns, rid = _ligne_avec_relique()
    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, payload)

    msg = str(e.value)
    assert "ne détruirait RIEN" in msg
    assert "rien n'a été écrit" in msg
    assert "data_drop_column" in msg, "le refus nomme le geste qui ABOUTIT"
    assert "contact1_nom.link" in msg, "avec le nom LITTÉRAL, point compris"


def test_une_ecriture_ORDINAIRE_passe_toujours(live):
    """⚠️ La moitié qui garantit qu'on n'a pas cassé le geste courant. Sur le tableau
    des cinq cents, 500 lignes portent une relique et leurs agents écrivent dans la
    couche imbriquée chaque jour — la garde ne doit pas les arrêter."""
    st, ns, rid = _ligne_avec_relique()
    st.update_row(ns, rid, {"contact1_nom": {"link": "https://nouveau"}})   # ne lève pas


def test_QUAND_LES_DEUX_COEXISTENT_la_lecture_sert_la_RELIQUE(live):
    """⚠️ Fait mesuré en écrivant ce banc, et personne ne l'avait vu : une fois les
    deux formes présentes, la lecture à plat rend la RELIQUE et masque la couche qu'on
    vient d'écrire. Les deux produisent le même nom (`contact1_nom.link`), et c'est la
    clé littérale qui gagne.

    Ce banc ne dit pas que c'est bien — il FIGE ce qui est, pour qu'une migration
    sache ce qu'elle manipule. Écrire l'imbriqué sur une ligne qui porte la relique ne
    change donc rien à ce qu'un lecteur voit : la valeur neuve est invisible tant que
    la relique est là. C'est la raison de l'ordre imposé à la réimbrication — écrire,
    PUIS purger le nom littéral — et la preuve qu'on ne peut pas s'arrêter au milieu."""
    st, ns, rid = _ligne_avec_relique()
    st.update_row(ns, rid, {"contact1_nom": {"link": "https://nouveau"}})

    assert st.list_rows(ns)[0]["contact1_nom.link"] == "https://profil", (
        "la relique masque la couche fraîchement écrite")


def test_un_effacement_SANS_relique_passe(live):
    """La garde ne doit mordre que là où le danger existe — sinon elle empêcherait
    d'effacer une couche normale."""
    from oto_mcp import db

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"}, {"key": "c", "type": "text"}]})
    rid = st.append_row(ns, {"siren": "1", "c": {"valeur": "v", "link": "L"}})["_id"]
    st.update_row(ns, rid, {"c": {"link": "@empty"}})   # ne lève pas


# ── la primitive, sur ses deux formes et son silence ─────────────────────────

def test_les_deux_formes_d_ecriture_sont_vues():
    avant = {"contact1_nom.link": "https://x"}
    assert rq.effacements_sur_relique({"contact1_nom": {"link": "@empty"}}, avant)
    assert rq.effacements_sur_relique({"contact1_nom.link": None}, avant)


def test_le_chemin_NOMINAL_ne_paie_rien():
    """Aucune relique sur la ligne : la garde sort avant de parcourir le payload."""
    assert rq.effacements_sur_relique({"a": {"comment": "@empty"}}, {"a": "x"}) == []
    assert rq.effacements_sur_relique({"a": None}, None) == []
