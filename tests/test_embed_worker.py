"""Le worker d'indexation sémantique ne paie qu'une fois ce qu'il indexe — `embed_worker`.

Novembre 2026 : 6,4 milliards de tokens Mistral en 23 jours (59 millions en octobre), et
aucun vecteur écrit depuis le 19/11. La file se lisait par id croissant, 16 pages par
tour, tous leurs morceaux embarqués d'un bloc : un morceau refusé (400) faisait jeter
tout le lot APRÈS que les requêtes précédentes avaient été payées, et le même lot
repartait 20 s plus tard. Ces bancs tiennent chaque garde qui ferme cette boucle.

Le fournisseur est doublé (`embeddings.embed_requete`) : il compte ce qu'on lui envoie
et facture un token par octet. Tout le reste tourne sur une VRAIE base (`live`) : le
bail, l'empreinte et le compte du jour vivent dans PostgreSQL."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp import db, embed_worker as W, embeddings as E
from oto_mcp.db import embed_file

psycopg = pytest.importorskip("psycopg")


# ══ 1. le découpage : une borne en octets, sûre quel que soit le texte ═════════

@pytest.mark.parametrize("texte", [
    "é" * 20000,                                    # 2 octets par caractère, sans espace
    "数据" * 9000,                                   # 3 octets par caractère
    ("{\"k\":\"v\"} " * 3000),                       # JSON dense
    "\n\n".join("mot " * 400 for _ in range(30)),    # prose en paragraphes
])
def test_aucun_morceau_ne_depasse_la_borne_en_octets(texte):
    morceaux = E.chunks(texte)
    assert morceaux and all(len(m.encode()) <= E.MAX_OCTETS_MORCEAU for m in morceaux)
    assert "".join(morceaux).replace("\n", "").replace(" ", "") == \
        texte.replace("\n", "").replace(" ", "")
    for lot in E.lots(morceaux):
        assert sum(len(m.encode()) for m in lot) <= E.MAX_OCTETS_REQUETE


def test_une_requete_hors_borne_n_est_pas_envoyee(monkeypatch):
    monkeypatch.setattr(E.httpx, "Client", lambda **k: pytest.fail("envoyée"))
    with pytest.raises(ValueError):
        E.embed_requete(["x" * (E.MAX_OCTETS_MORCEAU + 1)])
    with pytest.raises(ValueError):
        E.embed_requete(["x" * 5000, "y" * 5000])


def test_une_retouche_ne_change_que_son_morceau():
    paras = [f"paragraphe {i} " + "texte " * 800 for i in range(6)]
    avant = E.chunks("\n\n".join(paras))
    paras[-1] += " retouche"
    apres = E.chunks("\n\n".join(paras))
    assert len(avant) > 2 and avant[:-1] == apres[:-1] and avant[-1] != apres[-1]


@pytest.mark.parametrize("brut", ["abc", "-1", "1e6"])
def test_un_plafond_illisible_leve(monkeypatch, brut):
    monkeypatch.setenv("OTO_EMBED_PLAFOND_TOKENS_JOUR", brut)
    with pytest.raises(E.ReglagePlafondInvalide):
        E.plafond_jour()


def test_le_plafond_par_defaut(monkeypatch):
    monkeypatch.delenv("OTO_EMBED_PLAFOND_TOKENS_JOUR", raising=False)
    assert E.plafond_jour() == E.PLAFOND_DEFAUT == 5_000_000


def test_l_attente_double_quand_le_fournisseur_est_indisponible():
    indispo = W.Bilan(arret="HTTP 429", fournisseur_indisponible=True)
    attentes, a = [], W._POLL_S
    for _ in range(10):
        a = W.prochaine_attente(a, indispo)
        attentes.append(a)
    assert attentes[:3] == [40, 80, 160] and attentes[-1] == W._ATTENTE_MAX_S
    assert W.prochaine_attente(a, W.Bilan(indexees=1)) == W._POLL_S
    assert W.prochaine_attente(a, W.Bilan(arret="plafond journalier atteint")) == W._POLL_S


def test_le_worker_tourne_sous_le_verrou_de_boucle(monkeypatch):
    """Un seul processus joue un tour : la boucle passe par `sous_verrou`, à son nom."""
    vus = []

    def sous_verrou(nom, fonction, *, si_saute):
        vus.append((nom, fonction))
        return si_saute

    class Fin(Exception):
        pass

    async def dormir(_):
        raise Fin

    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    monkeypatch.setattr(W.verrou_de_boucle, "sous_verrou", sous_verrou)
    monkeypatch.setattr(W.asyncio, "sleep", dormir)
    with pytest.raises(Fin):
        asyncio.run(W.run_embed_loop())
    assert vus == [("embed_worker", W._tour)]


# ══ 2. sur une vraie base ═════════════════════════════════════════════════════

_POISON = "POISON"


class Fournisseur:
    """Le double de Mistral : compte les requêtes, facture un token par octet, refuse
    (400) un texte qui porte `POISON`, ou rend `passager` (429) tant qu'on le demande."""

    def __init__(self):
        self.requetes: list[list[str]] = []
        self.passager = False
        self.pendant = None

    def __call__(self, textes):
        self.requetes.append(list(textes))
        if self.pendant is not None:
            rappel, self.pendant = self.pendant, None
            rappel()
        if self.passager:
            raise E.EchecEmbedding("HTTP 429 : rate limited", definitif=False)
        if any(_POISON in t for t in textes):
            raise E.EchecEmbedding("HTTP 400 : Too many tokens", definitif=True)
        return [[0.01] * E.DIM for _ in textes], sum(len(t.encode()) for t in textes)

    @property
    def tokens(self) -> int:
        return sum(len(t.encode()) for r in self.requetes for t in r)

    def textes(self) -> list[str]:
        return [t for r in self.requetes for t in r]


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE docs SET embed_dirty = FALSE")
        conn.execute("UPDATE projects SET embed_dirty = FALSE")
        conn.execute("DELETE FROM embed_sources")
        conn.execute("DELETE FROM embed_consommation")
    f = Fournisseur()
    monkeypatch.setattr(W.embeddings, "embed_requete", f)
    # Les pages seules : la base de test sème des guides à indexer, qui partageraient
    # le compte des requêtes. Le mécanisme est le même pour chaque sorte de source.
    monkeypatch.setattr(W, "sources", lambda: (W._docs(),))
    monkeypatch.delenv("OTO_EMBED_PLAFOND_TOKENS_JOUR", raising=False)
    monkeypatch.setenv("OTO_EMBED_STABILITE_S", "0")
    alertes = []
    monkeypatch.setattr(W, "_alerter_equipe", lambda c, p: alertes.append((c, p)))
    pid = db.create_project("user", f"u_embed_{uuid.uuid4().hex[:6]}", "indexation")
    return {"f": f, "pid": pid, "alertes": alertes}


def _page(monde, *paragraphes: str) -> int:
    return db.create_doc(monde["pid"], f"page {uuid.uuid4().hex[:6]}",
                         body_md="\n\n".join(paragraphes))


def _para(lettre: str, n: int = 4500) -> str:
    """Un paragraphe de `n` octets : seul dans son morceau et dans sa requête."""
    return (lettre + " ") * (n // 2)


def _dirty(doc_id: int) -> bool:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("SELECT embed_dirty FROM docs WHERE id = %s",
                            (doc_id,)).fetchone()["embed_dirty"]


def _rangs(doc_id: int) -> list[int]:
    return sorted(db.doc_morceaux_stockes(doc_id))


def test_un_400_sur_un_morceau_ne_relance_pas_la_page(monde):
    f = monde["f"]
    page = _page(monde, _para("a"), _para("b") + _POISON, _para("c"))
    W._tour()
    # Le morceau 0 a été payé puis gardé ; le refus sort la page de la file, raison notée.
    assert _rangs(page) == [0]
    assert not _dirty(page)
    assert "400" in embed_file.echec("doc", str(page))["echec_raison"]
    payes = len(f.requetes)
    for _ in range(3):
        W._tour()
    assert len(f.requetes) == payes, "la page refusée est repartie au tour suivant"
    # Remarquée sans que son texte change : rien ne part.
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE docs SET embed_dirty = TRUE WHERE id = %s", (page,))
    W._tour()
    assert len(f.requetes) == payes and not _dirty(page)


def test_la_cause_du_19_11_un_lot_paye_puis_un_400_ne_se_repaie_pas(monde):
    """La boucle relevée en prod du 19 au 23/11 : chaque tour payait 76 à 80 requêtes
    sur le lot des 16 premières pages de la file, puis recevait un 400 déterministe sur
    un morceau ; tout le lot était jeté sans rien écrire, et le même lot repartait au
    tour suivant (~78 tours par heure). Ici : un lot de pages payé en entier, puis un
    400 sur le dernier morceau de la dernière page. Ce qui a été payé est écrit, et le
    tour suivant ne paie plus rien."""
    f = monde["f"]
    saines = [_page(monde, *(_para(chr(ord("a") + j)) + f" {i}" for j in range(4)))
              for i in range(5)]
    empoisonnee = _page(monde, _para("x"), _para("y"), _para("z") + _POISON)
    W._tour()
    assert len(f.requetes) == 5 * 4 + 3                  # tout le lot payé, puis le 400
    assert all(_rangs(p) == [0, 1, 2, 3] and not _dirty(p) for p in saines)
    assert _rangs(empoisonnee) == [0, 1] and not _dirty(empoisonnee)
    textes = f.textes()
    assert len(textes) == len(set(textes)), "un morceau payé deux fois"
    assert embed_file.jour()["tokens"] == sum(
        len(t.encode()) for r in f.requetes[:-1] for t in r)   # le 400 n'est pas compté
    payes = len(f.requetes)
    for _ in range(5):
        W._tour()
    assert len(f.requetes) == payes, "le même lot s'est repayé au tour suivant"


def test_une_ecriture_ratee_apres_l_appel_paye_ne_repaie_pas_en_boucle(monde, monkeypatch):
    """L'autre forme possible du 19/11 : l'appel est payé, puis l'écriture en base échoue.
    La tentative est comptée ; au-delà de la borne, la page sort de la file."""
    f = monde["f"]
    page = _page(monde, _para("a"))

    def ecriture_ratee(*a, **k):
        raise psycopg.errors.DataException("écriture refusée")

    monkeypatch.setattr(W.db, "poser_morceau_doc", ecriture_ratee)
    for _ in range(W._TENTATIVES_MAX + 5):
        W._tour()
    assert len(f.requetes) == W._TENTATIVES_MAX
    assert not _dirty(page)
    assert "DataException" in embed_file.echec("doc", str(page))["echec_raison"]
    # Ce qui a été payé est compté contre le plafond, même sans écriture.
    assert embed_file.jour()["tokens"] == f.tokens


def test_un_fournisseur_indisponible_ne_sort_pas_la_page(monde):
    """429, 5xx, réseau : non facturé, rien ne se compte contre la page ; le tour
    s'arrête (l'attente double, `prochaine_attente`) et la page reste dans la file."""
    f = monde["f"]
    page = _page(monde, _para("a"))
    f.passager = True
    for _ in range(W._TENTATIVES_MAX + 2):
        b = W._tour()
        assert b.fournisseur_indisponible
    etat = embed_file.echec("doc", str(page))
    assert _dirty(page) and etat["echec_raison"] is None and etat["tentatives"] == 0
    f.passager = False
    W._tour()
    assert _rangs(page) == [0] and not _dirty(page)


def test_un_400_sur_une_requete_a_plusieurs_morceaux_renvoie_chacun_seul(monde):
    f = monde["f"]
    page = _page(monde, _para("a", 3000), _para("b", 3000) + _POISON, _para("c", 3000))
    W._tour()
    # [a+b] refusé ensemble → a seul passe, b seul est refusé : la page sort, a est gardé.
    assert _rangs(page) == [0] and not _dirty(page)
    assert [len(r) for r in f.requetes] == [2, 1, 1]


def test_le_demarrage_ne_remet_pas_a_indexer_une_page_en_echec(monde):
    from oto_mcp.db._conn import _connect
    from oto_mcp.db import init_db
    f = monde["f"]
    page = _page(monde, _para("a") + _POISON)
    W._tour()
    assert not _dirty(page) and _rangs(page) == []
    init_db()
    assert not _dirty(page), "le démarrage remet en file une page sortie en échec"
    with _connect() as conn:   # la page réécrite revient, elle
        conn.execute("DELETE FROM embed_sources")
    init_db()
    assert _dirty(page)


def test_une_page_n_est_indexee_que_stable(monde, monkeypatch):
    monkeypatch.setenv("OTO_EMBED_STABILITE_S", "600")
    page = _page(monde, "court")
    W._tour()
    assert monde["f"].requetes == [] and _dirty(page)
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE docs SET updated_at = NOW() - INTERVAL '11 minutes' "
                     "WHERE id = %s", (page,))
    W._tour()
    assert len(monde["f"].requetes) == 1 and not _dirty(page)


def test_un_morceau_inchange_n_est_pas_reembarque(monde):
    f = monde["f"]
    paras = [_para(chr(ord("a") + i)) for i in range(4)]
    page = _page(monde, *paras)
    W._tour()
    assert _rangs(page) == [0, 1, 2, 3] and not _dirty(page)
    f.requetes.clear()
    paras[2] += " retouche"
    db.update_doc(page, body_md="\n\n".join(paras))
    assert _dirty(page)
    W._tour()
    assert len(f.textes()) == 1 and "retouche" in f.textes()[0]
    assert _rangs(page) == [0, 1, 2, 3] and not _dirty(page)
    # Un paragraphe retiré : les morceaux qui suivent changent de rang et reprennent
    # leur vecteur, sans requête.
    f.requetes.clear()
    db.update_doc(page, body_md="\n\n".join(paras[:1] + paras[2:]))
    W._tour()
    assert f.requetes == [] and _rangs(page) == [0, 1, 2] and not _dirty(page)


def test_deux_processus_n_indexent_pas_la_meme_page(monde):
    """Le verrou de boucle perdu en plein tour (connexion rompue) : un second processus
    joue son tour pendant que le premier paie. Le bail en base lui cache la page."""
    f = monde["f"]
    page = _page(monde, _para("a"), _para("b"))
    second = {}

    def tour_du_second_processus():
        assert db.list_dirty_docs(16) == []
        assert embed_file.reserver("doc", [str(page)], 600) == set()
        second["bilan"] = W._tour()

    f.pendant = tour_du_second_processus
    W._tour()
    assert second["bilan"].indexees == 0
    textes = f.textes()
    assert len(textes) == 2 and len(set(textes)) == 2
    assert _rangs(page) == [0, 1] and not _dirty(page)
    assert embed_file.echec("doc", str(page)) is None   # succès : l'état s'efface


def test_le_plafond_coupe_pour_la_journee_et_alerte_une_fois(monde, monkeypatch):
    f = monde["f"]
    monkeypatch.setenv("OTO_EMBED_PLAFOND_TOKENS_JOUR", "6000")
    page = _page(monde, _para("a"), _para("b"))
    W._tour()
    assert len(f.requetes) == 1                     # la seconde dépasserait le plafond
    assert _rangs(page) == [0] and _dirty(page)     # ce qui est payé reste, la page aussi
    assert embed_file.jour() == {"tokens": f.tokens, "plafond_atteint": True}
    assert len(monde["alertes"]) == 1
    autre = _page(monde, "court")
    for _ in range(3):
        W._tour()
    assert len(f.requetes) == 1 and _dirty(autre)   # arrêté pour la journée
    assert len(monde["alertes"]) == 1               # alerté une fois


def test_un_plafond_nul_n_embarque_rien(monde, monkeypatch):
    monkeypatch.setenv("OTO_EMBED_PLAFOND_TOKENS_JOUR", "0")
    page = _page(monde, "court")
    W._tour()
    assert monde["f"].requetes == [] and _dirty(page)


def test_briefs_et_guides_ne_se_paient_qu_une_fois(monde, monkeypatch):
    """Les sources à un vecteur (brief, couche de contexte) passent par le même chemin."""
    monkeypatch.setattr(W, "sources", lambda: (W._aux("brief"), W._aux(db.NODE_KIND)))
    f = monde["f"]
    pid = db.create_project("user", f"u_embed_{uuid.uuid4().hex[:6]}", "brief",
                            brief_md="Le brief " + _para("z", 9000))
    W._tour()
    assert db.get_aux_embedding_sha("brief", pid) is not None
    payes = len(f.requetes)
    assert payes >= 1 and all(len(t.encode()) <= E.MAX_OCTETS_MORCEAU for t in f.textes())
    W._tour()
    assert len(f.requetes) == payes


def test_bleu_et_vert_sur_la_meme_base_un_seul_appelle(monde):
    """22/11, 21:00-21:17 UTC : les deux couleurs d'une bascule ont fait tourner le
    worker ensemble, et les deux ont payé. Le verrou de boucle vit dans PostgreSQL :
    la couleur qui ne l'obtient pas saute son tour sans appeler le fournisseur."""
    import threading
    f = monde["f"]
    page = _page(monde, _para("a"), _para("b"))
    vert = {}

    def tour_du_vert():
        # Une autre connexion, comme un autre processus : le verrou est pris.
        t = threading.Thread(target=lambda: vert.update(
            r=W.verrou_de_boucle.sous_verrou(W.NOM, W._tour, si_saute="sauté")))
        t.start()
        t.join()

    f.pendant = tour_du_vert
    bleu = W.verrou_de_boucle.sous_verrou(W.NOM, W._tour, si_saute="sauté")
    assert vert["r"] == "sauté" and bleu.indexees == 1
    assert len(f.textes()) == 2 and len(set(f.textes())) == 2
    assert _rangs(page) == [0, 1]
