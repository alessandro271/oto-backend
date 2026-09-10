"""Une procédure archivée peut revenir EN SERVICE, et le geste dit ce qu'il défait
(#857) — contre un vrai PostgreSQL, parce que la subtilité est dans le SQL.

L'inverse de l'archivage n'existait pas, et c'était un choix ASSUMÉ : parité avec
les projets, dont l'archivage n'a pas d'inverse non plus. **La parité se rompt ici
pour les procédures seules, sur décision d'Alexis du 10/09/2026** — les projets
gardent le trou et sont instruits à part. Lire cette fonction comme une incohérence
serait ignorer qu'elle en est une, voulue.

Ce que la mesure a montré et qui a emporté la décision : 3 procédures archivées sur
238 en production, dont **deux réécrites après coup** par des clients qui les
croyaient en service. Sans désarchivage, refuser ces écritures les aurait enfermés —
plus d'édition, pas de remise en service, et la suppression pour seule sortie, donc
la destruction de l'historique que l'archivage existe pour préserver.

⚠️ **Le piège est dans le SQL, et j'y suis tombé en écrivant le commentaire qui le
dénonce.** `RETURNING archived_at` après un `SET archived_at = NULL` rend la valeur
NEUVE : la fonction aurait annoncé « rien à défaire » chaque fois qu'elle venait de
défaire quelque chose. La date d'avant est lue par une CTE dans la même instruction
— ni relecture préalable (une autre session l'aurait changée entre-temps), ni
relecture après coup (elle n'existe plus).

Éprouvé rouge le 2026-09-10, contre la vraie base et non par raisonnement : un
`UPDATE … SET archived_at = NULL … RETURNING archived_at` rend bien `None`. La
version fautive aurait donc annoncé « rien à défaire » juste après avoir défait
quelque chose, et le deuxième test le nomme.
"""
from __future__ import annotations

import psycopg
import pytest

from oto_mcp import org_store


def _ddl() -> str:
    """Le `CREATE TABLE` de la table, plus la colonne que seul un `ALTER` pose.

    ⚠️ **`archived_at` n'est PAS dans le `CREATE TABLE`** : c'est une colonne de
    migration vivante, ajoutée par un `ALTER` au démarrage. Un banc qui rejoue le
    seul `CREATE` obtient donc une table où la colonne n'existe pas — le piège que
    la carte du dépôt documente, et celui sur lequel ce fichier a rougi d'abord.
    On la repose ici explicitement plutôt que de rejouer toute la séquence de
    migration, dont ce banc n'a pas besoin."""
    from oto_mcp.db import _schema
    s = _schema._SCHEMA
    i = s.index("CREATE TABLE IF NOT EXISTS org_instructions")
    creation = s[i:s.index("\n);", i) + 3]
    # ⚠️ **La référence croisée est RETIRÉE, sinon le montage exige `orgs`.** Ce
    # banc n'a besoin que de cette table ; la base de ce poste porte tout le schéma
    # et masquait donc le manque, alors que le PostgreSQL de la CI part NU — huit
    # erreurs au montage, tronc rouge, et un verdict local vert qui ne pouvait pas
    # le voir. Même geste que le banc de tri du datastore, pour la même raison.
    creation = creation.replace(" REFERENCES orgs(id) ON DELETE CASCADE", "")
    # Les colonnes posées par une migration, reprises de `db/_init.py` et non
    # devinées : `id` et `archived_at` y sont ajoutées par `ALTER`, donc un banc qui
    # rejoue le seul `CREATE` obtient une table où elles n'existent pas. C'est le
    # piège que la carte du dépôt documente, et ce fichier a rougi dessus deux fois.
    return creation + (
        "\nALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS id BIGSERIAL;"
        "\nALTER TABLE org_instructions "
        "ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;"
        "\nALTER TABLE org_instructions "
        "ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb;"
        "\nALTER TABLE org_instructions "
        "ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'org';"
        "\nALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS owner_id TEXT;")


@pytest.fixture()
def pg(pg_module_dsn, monkeypatch):
    """Une base NEUVE pour ce module — donc NUE, comme celle de la CI.

    ⚠️ **`pg_dsn` désigne une base partagée qui porte déjà tout le schéma**, et
    c'est elle qui a masqué le défaut : ce banc passait ici et posait huit erreurs
    au montage en CI (`relation "orgs" does not exist`), tronc rouge. Monter sur une
    base neuve reproduit le manque localement au lieu de le découvrir en
    intégration — un vert obtenu sur une base déjà meublée ne dit rien du montage.
    Et ça évite au passage d'écrire dans la base des autres sessions.
    """
    pg_dsn = pg_module_dsn
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    from oto_mcp.db import _conn
    monkeypatch.setattr(_conn, "_database_url", lambda: pg_dsn)
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS org_instructions")
        c.execute(_ddl())
        yield c
        c.execute("DROP TABLE IF EXISTS org_instructions")


def _pose(pg, slug="target-ownership-register", archivee=False):
    pg.execute(
        "INSERT INTO org_instructions (owner_type, owner_id, slug, title, body_md, "
        "version, archived_at) VALUES ('org','231',%s,'T','corps',4,"
        + ("NOW()" if archivee else "NULL") + ")", (slug,))


def test_une_archivee_revient_en_service(pg):
    _pose(pg, archivee=True)
    assert org_store.unarchive_instruction("org", 231, "target-ownership-register")
    reste = pg.execute(
        "SELECT archived_at FROM org_instructions WHERE slug = %s",
        ("target-ownership-register",)).fetchone()
    assert reste[0] is None, "la procédure doit être de nouveau en service"


def test_le_geste_DIT_ce_qu_il_a_defait(pg):
    """Le garde-fou qui compte. Un geste qui ressuscite une ligne retirée exprès
    doit laisser savoir QUAND elle l'avait été — sinon le journal dit qu'on a agi,
    pas ce qu'on a annulé."""
    _pose(pg, archivee=True)
    avant = org_store.unarchive_instruction("org", 231, "target-ownership-register")
    assert avant is not None, (
        "la date d'archivage est perdue : `RETURNING` a rendu la valeur NEUVE")
    # ⚠️ On garde qu'une DATE revient, pas son TYPE Python : ce store sérialise ses
    # horodatages en chaînes (c'est ce que les surfaces servent déjà), et exiger un
    # `datetime` ferait rougir ce banc sur un contrat que personne ne rend. Ma
    # première version l'exigeait — c'est en le vérifiant que j'ai lu le contrat.
    assert str(avant).startswith("20"), f"ce n'est pas un horodatage : {avant!r}"
    assert len(str(avant)) >= 10, "une date tronquée ne trace rien"


def test_remettre_en_service_ce_qui_l_EST_DEJA_est_un_non_geste(pg):
    """Ni faute, ni silence trompeur : `None` dit « rien à défaire », et aucune
    exception ne vient punir un appel inoffensif."""
    _pose(pg, archivee=False)
    assert org_store.unarchive_instruction("org", 231, "target-ownership-register") is None


def test_une_procedure_INCONNUE_ne_leve_pas(pg):
    assert org_store.unarchive_instruction("org", 231, "jamais-existe") is None


def test_le_desarchivage_ne_touche_QUE_la_procedure_visee(pg):
    """Sur des données de clients, un geste trop large est le risque réel : deux
    procédures archivées, une seule remise en service."""
    _pose(pg, slug="une", archivee=True)
    _pose(pg, slug="deux", archivee=True)
    org_store.unarchive_instruction("org", 231, "une")
    etats = dict(pg.execute(
        "SELECT slug, archived_at IS NULL FROM org_instructions ORDER BY slug").fetchall())
    assert etats == {"une": True, "deux": False}


def test_il_ne_traverse_PAS_les_organisations(pg):
    """Le slug seul ne désigne rien : deux orgs peuvent porter le même."""
    _pose(pg, slug="partage", archivee=True)
    pg.execute(
        "INSERT INTO org_instructions (owner_type, owner_id, slug, title, body_md, "
        "version, archived_at) VALUES ('org','999','partage','T','corps',1,NOW())")
    org_store.unarchive_instruction("org", 231, "partage")
    autre = pg.execute(
        "SELECT archived_at FROM org_instructions WHERE owner_id = '999'").fetchone()
    assert autre[0] is not None, "l'org voisine a été touchée"



def test_la_lecture_par_slug_REMONTE_l_etat_d_archivage(pg):
    """La moitié du défaut que personne n'avait vue : la lecture par slug n'a AUCUN
    filtre sur l'archivage — elle sert une procédure retirée comme une procédure en
    service — et elle ne remontait même pas la colonne. Elle ne pouvait donc pas
    dire l'état, quelle que soit la surface au-dessus."""
    _pose(pg, archivee=True)
    lu = org_store.get_instruction("org", 231, "target-ownership-register")
    assert lu is not None, "la lecture par slug sert l'archivée : c'est le cas mesuré"
    assert lu.get("archived_at") is not None, (
        "sans cette colonne, aucune surface ne peut annoncer l'état")


def test_une_procedure_EN_SERVICE_rend_un_etat_vide(pg):
    _pose(pg, archivee=False)
    lu = org_store.get_instruction("org", 231, "target-ownership-register")
    assert lu["archived_at"] is None
