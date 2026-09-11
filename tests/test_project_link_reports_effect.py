"""oto#119 — `op=link` disait qu'il avait RÉUSSI, jamais ce qu'il avait FAIT.

Le geste est un `INSERT … ON CONFLICT DO UPDATE` : idempotent, rejouable, et c'est
bien ainsi. Ce qui manquait est son COMPTE RENDU. Mesuré en prod : la même session a
rejoué le rattachement trois fois en trois minutes — trois liens créés au premier
passage, aucun aux deux suivants, **trois réponses identiques** (succès + la liste
complète des liens) et une ligne au journal d'activité à chaque fois.

Conséquence : un appelant automatique chargé de « s'assurer qu'une ressource est
rattachée, et de la rattacher sinon » ne peut pas savoir s'il a agi. Il rapporte avoir
corrigé ce qui n'avait rien à corriger.

Deux partis pris, qui expliquent la forme de ce fichier :

1. **Contre un vrai PostgreSQL.** La distinction créé / déjà là ne vient pas d'une
   branche Python : elle vient de ce que l'`ON CONFLICT` a fait de la ligne. Une
   doublure qui « imiterait l'upsert » testerait mon imitation, pas le SQL.
2. **Sur le CHEMIN SERVI.** Les gestes passent par la capacité `me.project`, avec son
   autz DÉCLARÉE jouée sur un `RawCtx` — le chemin de l'outil `oto_project` et de
   `POST /api/me/projects` — et n'assertent que sur ce que la surface EXPOSE. C'est la
   réponse qui était indistincte, pas la fonction de base : l'assert doit donc porter
   là où l'appelant regarde.

Chaque test travaille sur SA propre référence de lien, pour qu'aucun ne dépende de
l'ordre d'exécution ni de ce qu'un autre a laissé en base.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp.capabilities._types import RawCtx


@pytest.fixture(scope="module")
def monde(pg_dsn):
    """Une base JETABLE à nous, bootée par le vrai `init_db`, avec une org, son admin,
    une procédure et le projet auquel on la rattache.

    ⚠️ Base à part et pas celle du conteneur partagé (`pg_dsn` est session-scopé) : un
    boot complet y laisse ses tables et leurs FK, et les tests qui recréent deux tables
    autonomes n'y arrivent plus. Même recette que `test_procedure_paliers_681`."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_119_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    dbconn._pool = None
    try:
        from oto_mcp import db, org_store
        from oto_mcp.db import init_db
        init_db()
        org = org_store.create_org("Acme", created_by="u-admin")
        org_store.add_org_member(org, "u-admin", "org_admin")
        org_store.set_active_org("u-admin", org)
        org_store.set_instruction("org", org, "qualification", "1. Vérifier le SIREN",
                                  title="Qualification", set_by="u-admin")
        proc = org_store.get_instruction("org", org, "qualification")
        projet = db.create_project("org", str(org), "Développement commercial",
                                   created_by="u-admin")
        yield {"org": org, "projet": projet, "proc": str(proc["id"])}
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _link(monde, ref: str, **args) -> dict:
    """UN `oto_project op=link` par le chemin servi : autz déclarée, puis handler."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.project")
    inp = cap.Input(op="link", project_id=monde["projet"], target_type="procedure",
                    target_ref=ref, **args)
    return cap.handler(cap.authz(RawCtx(sub="u-admin"), inp), inp)


def _ref() -> str:
    """Une référence de procédure à moi seul. Numérique : l'ADR 0032 a fixé l'id comme
    référence stable, et le handler laisse passer les chiffres sans les résoudre."""
    return str(900_000_000 + uuid.uuid4().int % 10_000_000)


# ── Le défaut, tel qu'il se vit : le même geste, deux fois ───────────────────

def test_rejouer_le_geste_ne_rend_plus_la_meme_reponse(monde):
    """**Le test du défaut.** Deux appels identiques, à la suite. Avant le correctif
    les deux réponses étaient ÉGALES — succès, et la liste complète des liens dans les
    deux cas. L'appelant ne pouvait pas distinguer « je viens de le rattacher » de
    « il l'était déjà », donc ne pouvait pas rendre compte de ce qu'il avait fait."""
    premier = _link(monde, monde["proc"], label="Qualification")
    second = _link(monde, monde["proc"], label="Qualification")

    assert premier["ok"] is True and second["ok"] is True
    assert premier["link_status"] == "created"
    assert second["link_status"] == "unchanged"
    assert premier != second, "les deux réponses sont encore indistinctes"


def test_les_liens_rendus_eux_ne_disent_toujours_rien(monde):
    """Le corollaire, et la raison pour laquelle l'appelant ne pouvait pas s'en tirer
    par ses propres moyens : la partie de la réponse qui décrit l'ÉTAT — `links`, avec
    jusqu'à la date de création de chaque ligne — est rigoureusement la même après un
    rattachement effectif et après un rejeu. Seul `link_status` porte l'événement."""
    ref = _ref()
    _link(monde, ref, label="Relance")
    a = _link(monde, ref, label="Relance")
    b = _link(monde, ref, label="Relance")

    assert a["links"] == b["links"]
    assert a["link_status"] == b["link_status"] == "unchanged"


def test_une_vraie_reecriture_se_nomme_et_dit_ses_champs(monde):
    """Le troisième cas, entre les deux : le lien existait, et cet appel l'a CHANGÉ.
    Dire « created » serait faux, dire « unchanged » aussi — et un appelant qui
    corrige un libellé a besoin de savoir qu'il a écrit, et sur quoi."""
    ref = _ref()
    _link(monde, ref, label="Clôture")
    out = _link(monde, ref, label="Clôture annuelle")

    assert out["link_status"] == "updated"
    assert out["changed_fields"] == ["label"]
    lien = next(l for l in out["links"] if l["target_ref"] == ref)
    assert lien["label"] == "Clôture annuelle"


def test_sans_reecriture_aucun_champ_nest_annonce(monde):
    """`changed_fields` n'apparaît QUE sur une vraie réécriture : une liste vide sur un
    rejeu se lirait comme « j'ai écrit, mais rien n'a bougé », soit un troisième
    message pour un non-événement."""
    ref = _ref()
    _link(monde, ref, label="Veille")
    out = _link(monde, ref, label="Veille")

    assert out["link_status"] == "unchanged"
    assert "changed_fields" not in out


# ── Sous la surface : ce que le SQL sait, et ce qu'il ignore ─────────────────

def test_le_delta_porte_sur_des_valeurs_pas_sur_une_intention(monde):
    """`changed` est calculé en confrontant l'état d'AVANT (la CTE, sous le MÊME
    snapshot que l'écriture) à celui d'APRÈS. Reposer le même `role` ne le déclare
    donc pas changé, et réécrire trois champs les nomme tous les trois."""
    from oto_mcp import db
    ref = _ref()

    assert db.add_project_link(monde["projet"], "procedure", ref, "A",
                               role="le pourquoi") == {"status": "created",
                                                       "changed": []}
    assert db.add_project_link(monde["projet"], "procedure", ref, "A",
                               role="le pourquoi") == {"status": "unchanged",
                                                       "changed": []}
    assert db.add_project_link(monde["projet"], "procedure", ref, "B",
                               role="un autre pourquoi",
                               config={"x": 1}) == {"status": "updated",
                                                    "changed": ["label", "role",
                                                                "config"]}


def test_un_champ_tu_nest_ni_efface_ni_compte_comme_un_changement(monde):
    """Le contrat d'idempotence d'avant est INTACT : `role`/`config`/`slot` omis ne
    sont pas écrasés (`COALESCE`). Un re-lien qui les tait n'écrit donc rien — et
    l'annoncer comme une mise à jour serait le même mensonge que l'inverse."""
    from oto_mcp import db
    ref = _ref()
    db.add_project_link(monde["projet"], "procedure", ref, "A", role="le pourquoi",
                        config={"x": 1})

    effet = db.add_project_link(monde["projet"], "procedure", ref, "A")

    assert effet == {"status": "unchanged", "changed": []}
    lien = next(l for l in db.list_project_links(monde["projet"])
                if l["target_ref"] == ref)
    assert lien["role"] == "le pourquoi" and lien["config"]["x"] == 1


def test_un_autre_binding_de_la_meme_entite_est_une_creation(monde):
    """La clé du geste est le BINDING `(projet, type, ref, identité)`, pas l'entité.
    Lier le même connecteur sous une AUTRE identité CRÉE, même si « le connecteur est
    déjà lié » : c'est ce que l'appelant doit lire, sans quoi il conclurait n'avoir
    rien fait alors qu'il vient d'ouvrir un second binding."""
    from oto_mcp import db
    ref = "folk-" + uuid.uuid4().hex[:6]
    premier = db.add_project_link(monde["projet"], "connecteur", ref, "Folk",
                                  identity_ref=None)
    second = db.add_project_link(monde["projet"], "connecteur", ref, "Folk",
                                 identity_ref="acc-2")

    assert premier["status"] == "created" and second["status"] == "created"


# ── Signal #883 : un champ tu, et un lien écrit sous l'ancienne graphie ──────

def _procedure(monde) -> tuple[str, str]:
    """Une procédure à moi seul, rendue en `(slug, id)` : les deux écritures qu'un lien
    a pu stocker selon qu'il est né avant ou après la normalisation en id."""
    from oto_mcp import org_store
    slug = "p-" + uuid.uuid4().hex[:8]
    org_store.set_instruction("org", monde["org"], slug, "1. Faire", title=slug,
                              set_by="u-admin")
    return slug, str(org_store.get_instruction("org", monde["org"], slug)["id"])


def _lies(out, *refs) -> list[dict]:
    return [l for l in out["links"] if l["target_ref"] in refs]


def test_relier_avec_le_seul_role_garde_le_libelle(monde):
    """Piège 1, vécu le 11/09/2026 : re-lier en ne passant que `role` rendait
    `changed_fields=[label, role]` et le libellé repartait à NULL — alors que le texte
    servi promet de préserver ce qu'on tait. Un champ omis n'est plus jamais écrasé."""
    ref = _ref()
    _link(monde, ref, label="Vivier", role="le pourquoi")
    out = _link(monde, ref, role="un autre pourquoi")

    assert out["link_status"] == "updated"
    assert out["changed_fields"] == ["role"]
    lien = _lies(out, ref)[0]
    assert lien["label"] == "Vivier" and lien["role"] == "un autre pourquoi"


@pytest.mark.parametrize("donnee", ["slug", "id"])
def test_un_lien_stocke_sous_son_slug_est_reecrit_pas_double(monde, donnee):
    """Piège 2 : le lien existant porte le SLUG (né avant la normalisation), `link`
    canonise la réf demandée en id et l'upsert, qui cherchait l'id, CRÉAIT un second
    lien, sans slot — que l'unlink retirait ensuite avec le premier. Les deux sens : la
    réf donnée par slug, et par id (le seul qui exerce la canonisation du STOCKÉ)."""
    from oto_mcp import db
    slug, pid = _procedure(monde)
    slot = "s_" + uuid.uuid4().hex[:8]
    db.add_project_link(monde["projet"], "procedure", slug, "Porte", slot=slot)

    out = _link(monde, slug if donnee == "slug" else pid, role="la garde d'achat")

    liens = _lies(out, slug, pid)
    assert [l["target_ref"] for l in liens] == [pid], "un second lien est né"
    assert liens[0]["slot"] == slot and liens[0]["label"] == "Porte"
    assert liens[0]["role"] == "la garde d'achat"
    assert out["link_status"] == "updated"
    assert out["changed_fields"] == ["target_ref", "role"]
    assert out["rewritten_from"] == slug


def test_deux_graphies_deja_liees_sont_dites_jamais_fondues(monde):
    """Le reliquat du défaut : un projet qui porte DÉJÀ les deux liens (le second créé
    avant ce correctif). Les fondre perdrait le slot de l'un en silence ; le lien met à
    jour celui par id, garde l'autre, et le DIT — l'appelant sait que `unlink` les
    retirera tous deux."""
    from oto_mcp import db
    slug, pid = _procedure(monde)
    db.add_project_link(monde["projet"], "procedure", slug, "Ancien",
                        slot="s_" + uuid.uuid4().hex[:8])
    db.add_project_link(monde["projet"], "procedure", pid, "Nouveau")

    out = _link(monde, pid, role="r")

    assert sorted(l["target_ref"] for l in _lies(out, slug, pid)) == sorted([slug, pid])
    assert out["duplicate_refs"] == [slug]
    assert f"« {slug} »" in out["warning"] and "unlink" in out["warning"]
    assert "rewritten_from" not in out
