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

import inspect

import psycopg
import pytest

from oto_mcp import org_store


def _ddl() -> str:
    """Le `CREATE TABLE` de la table, plus TOUTES ses migrations, relevées sur le
    code qui les exécute.

    ⚠️ **Énumérer les colonnes à la main ne tient pas** : `id`, `archived_at` puis
    `search_vec` ont manqué l'une après l'autre, chacune découverte par un rouge. Ce
    montage lit donc les `ALTER TABLE` de `db/_init.py` au lieu de les recopier —
    une colonne ajoutée demain suivra sans que ce banc ait à être retouché. Même
    parti que le banc d'ordre de démarrage : relever sur le SQL exécuté, pas sur une
    liste tenue à côté.

    ⚠️ La référence croisée est retirée du `CREATE` : le montage exigerait sinon la
    table `orgs`, dont ce banc n'a aucun besoin. C'est ce manque qui a rendu le tronc
    rouge — la base de ce poste le masquait, le PostgreSQL de la CI part nu.
    """
    import re
    from oto_mcp.db import _schema, _init
    s = _schema._SCHEMA
    def _table(nom: str) -> str:
        j = s.index(f"CREATE TABLE IF NOT EXISTS {nom}")
        brut = s[j:s.index("\n);", j) + 3]
        # Toute référence croisée est retirée : ce banc ne monte que ce qu'il exerce,
        # et exiger `orgs` est précisément ce qui a rendu le tronc rouge.
        return re.sub(r" REFERENCES \w+\([^)]*\)(?: ON DELETE CASCADE)?", "", brut)

    # L'écriture d'une procédure pousse l'état antérieur en RÉVISION : la table des
    # révisions fait donc partie du montage, sinon le banc du refus n'atteint jamais
    # son refus — il échoue sur une table absente, et accuse la mauvaise pièce.
    creation = _table("org_instructions") + "\n" + _table("org_instruction_revisions")
    src = inspect.getsource(_init)
    # ⚠️ **Dans l'ORDRE DU SOURCE, et c'est tout le sujet.** Regrouper les séquences
    # avant les colonnes casse : `CREATE SEQUENCE … OWNED BY org_instructions.id`
    # exige que `id` existe, et `ALTER COLUMN id SET DEFAULT nextval(…)` exige la
    # séquence. Ces deux instructions se tiennent l'une l'autre, et seule leur
    # séquence d'origine les satisfait — j'ai réordonné et je l'ai payé d'un rouge.
    # C'est le piège du démarrage que la carte du dépôt documente, en plus petit.
    pas = re.findall(
        r'"((?:ALTER TABLE org_instructions |CREATE SEQUENCE[^"]*org_instructions)'
        r'[^"]+)"', src)
    # La colonne de rang de recherche vient d'un AUTRE module (`db/search.py`), qui
    # la pose sur toutes les tables indexées : on la prend à sa source aussi, par sa
    # constante, plutôt que d'écrire son nom ici.
    from oto_mcp.db.search import RANK_VECTOR_COLUMN
    pas.append("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS "
               f"{RANK_VECTOR_COLUMN} tsvector")
    return creation + "".join(f"\n{q};" for q in pas)


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
        # Les DEUX tables : ne nettoyer que la première laissait les révisions
        # s'accumuler d'un test à l'autre, et la collision d'unicité qui en
        # sort accuse l'écriture testée au lieu du montage.
        c.execute("DROP TABLE IF EXISTS org_instruction_revisions")
        c.execute("DROP TABLE IF EXISTS org_instructions")
        c.execute(_ddl())
        yield c
        c.execute("DROP TABLE IF EXISTS org_instruction_revisions")
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


# ── le refus d'écrire, et sa sortie ──────────────────────────────────────────

def test_ecrire_sur_une_RETIREE_est_refuse(pg):
    """Le cas mesuré, dans l'autre sens : deux clients ont réécrit une procédure
    retirée en la croyant en service. L'écriture est désormais refusée."""
    from oto_mcp.org_store.instructions import InstructionArchived
    _pose(pg, archivee=True)
    with pytest.raises(InstructionArchived) as exc:
        org_store.set_instruction("org", 231, "target-ownership-register",
                                  "corps neuf", set_by="u1")
    assert "RETIRÉE" in str(exc.value)


def test_le_refus_NOMME_la_sortie(pg):
    """Le garde-fou qui empêche l'enfermement. Un refus sans issue laisserait la
    suppression pour seule sortie — donc la destruction de l'historique que
    l'archivage existe pour préserver."""
    from oto_mcp.org_store.instructions import InstructionArchived
    _pose(pg, archivee=True)
    with pytest.raises(InstructionArchived) as exc:
        org_store.set_instruction("org", 231, "target-ownership-register",
                                  "corps neuf", set_by="u1")
    msg = str(exc.value)
    assert "/unarchive" in msg, "le refus doit dire COMMENT remettre en service"
    assert "nouveau slug" in msg, "et l'autre voie, si elle doit rester retirée"


def test_le_refus_dit_la_CONSEQUENCE_pas_seulement_l_interdit(pg):
    """« Écrire dessus produirait une consigne que personne ne suit » : c'est ce qui
    fait comprendre pourquoi on refuse, au lieu de donner envie de contourner."""
    from oto_mcp.org_store.instructions import InstructionArchived
    _pose(pg, archivee=True)
    with pytest.raises(InstructionArchived) as exc:
        org_store.set_instruction("org", 231, "target-ownership-register", "x",
                                  set_by="u1")
    assert "personne ne suit" in str(exc.value)


def test_ecrire_sur_une_procedure_EN_SERVICE_passe_toujours(pg):
    """La contre-épreuve : le refus ne doit mordre que sur les retirées."""
    _pose(pg, archivee=False)
    v = org_store.set_instruction("org", 231, "target-ownership-register",
                                  "corps neuf", set_by="u1")
    assert v == 5, "la version monte normalement sur une procédure en service"


def test_remise_en_service_puis_ECRITURE_passe(pg):
    """Le parcours complet de sortie : on débloque, puis on écrit. C'est lui qui
    prouve que le refus n'enferme pas."""
    _pose(pg, archivee=True)
    assert org_store.unarchive_instruction("org", 231, "target-ownership-register")
    v = org_store.set_instruction("org", 231, "target-ownership-register",
                                  "corps neuf", set_by="u1")
    assert v == 5
