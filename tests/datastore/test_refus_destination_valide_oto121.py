"""Le refus d'un nom projeté nommait une destination elle-même refusée.

Une garde qui nomme sa destination vaut mieux qu'une garde muette — c'est le
principe du module. Mais une destination INVALIDE est pire que pas de destination :
elle fait dépenser un aller-retour, puis laisse l'appelant sans issue, avec
l'impression que le produit se contredit.

Le refus opposé à `contact1_nom` disait « écrire `contacts[0].nom` ». Cette forme est
rejetée deux gardes plus loin par `_refuse_dotted_names` : une adresse indexée est
une adresse de LECTURE, pas une clé d'écriture. Deux appels, deux refus, rien d'écrit.

Il n'existe aucune écriture au grain de l'élément aujourd'hui. Le refus le dit donc
franchement, et nomme le seul geste qui marche : reposer la colonne-liste ENTIÈRE,
couches réémises (oto#120).

⚠️ Le banc ENCHAÎNE réellement les deux appels — c'est le seul moyen de prouver
qu'une destination est valide. Une assertion sur le texte du message prouverait
seulement que le test est d'accord avec lui-même.
"""
from __future__ import annotations

import re
import uuid

import pytest

from oto_mcp.datastore.errors import RowValidationError

# Toute adresse indexée (`contacts[0].nom`, `contacts[3].email.comment`) : la forme
# que `_refuse_dotted_names` rejette, donc celle qu'aucun refus ne doit prescrire.
_ADRESSE_INDEXEE = re.compile(r"[A-Za-z_][\w-]*\[\d+\]\.")


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_c121_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "contacts", "type": "list", "flat_alias": "contact{n}_{attr}",
         "of": {"type": "object", "fields": [
             {"key": "nom", "type": "text"},
             {"key": "email", "type": "email"}]}},
    ],
}


@pytest.fixture
def table(live):
    """Un tableau en double-service, et une ligne dont un élément porte une couche."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = make_store("sub-test")
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "377768379", "contacts": [
        {"nom": "Ada", "nom.comment": "registre", "email": "ada@exemple.fr"}]})
    return st, ns, ns_id, row["_id"]


def _donnees(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _refus(st, ns, rid, payload) -> str:
    with pytest.raises((RowValidationError, ValueError)) as exc:
        st.update_row(ns, rid, payload)
    errs = getattr(exc.value, "errors", None)
    return " ".join(errs) if errs else str(exc.value)


# ══ le fait qui rendait l'indication invalide ═══════════════════════════════════

def test_une_adresse_indexee_est_bien_refusee_a_l_ecriture(table):
    """Le second refus — celui sur lequel retombait qui suivait l'indication. Il est
    JUSTE et ne bouge pas : c'est le premier message qui avait tort d'y envoyer."""
    st, ns, _ns_id, rid = table

    assert "n'est pas un nom de colonne" in _refus(
        st, ns, rid, {"contacts[0].nom": "Ada"})


# ══ le refus ne prescrit plus de cul-de-sac ═════════════════════════════════════

def test_le_refus_du_nom_projete_ne_nomme_aucune_adresse_indexee(table):
    """La garde mécanique : aucune forme `colonne[n].attr` dans le message, puisque
    aucune ne s'écrit. Elle attrape la régression sans relire la phrase."""
    st, ns, _ns_id, rid = table

    message = _refus(st, ns, rid, {"contact1_nom": "Ada"})

    assert "il ne s'écrit pas" in message, "le refus reste le même refus"
    assert not _ADRESSE_INDEXEE.search(message), \
        f"le refus prescrit une destination qui sera refusée : {message}"


def test_le_refus_dit_franchement_qu_aucun_element_ne_s_ecrit_seul(table):
    st, ns, _ns_id, rid = table

    message = _refus(st, ns, rid, {"contact1_nom": "Ada"})

    assert "ÉLÉMENT" in message and "n'est pas possible" in message
    assert "repose `contacts` ENTIÈRE" in message
    assert "nom.comment" in message, "la forme à réémettre, sinon les couches tombent"


def test_le_conseil_ne_double_pas_la_couche_du_nom_projete(table):
    """`contact1_nom.comment` résout vers l'attribut `nom.comment` : le conseil doit
    porter sur `nom`, sinon il prescrit `nom.comment.comment`. La composition du
    suffixe est le contrat de `resolve_flat_name`, pas un cas limite inventé."""
    st, ns, _ns_id, rid = table

    message = _refus(st, ns, rid, {"contact1_nom.comment": "registre"})

    assert "nom.comment.comment" not in message
    assert not _ADRESSE_INDEXEE.search(message)


# ══ l'enchaînement : le geste prescrit passe VRAIMENT ═══════════════════════════

def test_le_geste_que_le_refus_prescrit_ecrit_pour_de_bon(table):
    """Les deux appels enchaînés, sur la même ligne : on se fait refuser le nom
    projeté, on fait ce que le refus dit, et la ligne est écrite — couches
    comprises. C'est la seule preuve qu'une destination est valide."""
    st, ns, ns_id, rid = table

    message = _refus(st, ns, rid, {"contact1_nom": "ADA LOVELACE"})
    assert "repose `contacts` ENTIÈRE" in message

    st.update_row(ns, rid, {"contacts": [
        {"nom": "ADA LOVELACE", "nom.comment": "registre",
         "email": "ada@exemple.fr"}]})

    contacts = _donnees(ns_id, rid)["contacts"]
    assert contacts[0]["nom"] == {"valeur": "ADA LOVELACE", "comment": "registre"}
    assert "couches_effacees" not in st.off_schema_report(), \
        "le geste prescrit ne doit rien détruire — sinon le conseil serait faux"
