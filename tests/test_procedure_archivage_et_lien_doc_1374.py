"""Retirer une procédure sans la détruire, et binder le slot `doc` d'un projet.

Signaux #1374 et #1196 : `oto_procedure` ne savait que SUPPRIMER — un geste qui emporte
tout l'historique, réservé au chef d'équipe. L'archivage existait sur la face REST
(org_admin seul) et nulle part pour un agent. Les agents supprimaient faute de pouvoir
retirer. Signaux #1265, #1222, #1206 : une procédure peut déclarer un slot `doc`, mais
`oto_project op=link` n'acceptait aucun type pour le binder, et la purge du boot
effaçait de toute façon les liens `doc` à chaque démarrage — l'audit marquait donc ce
slot « non lié » pour toujours.

Sans base : chaque banc passe par le handler SERVI et n'assert que sur ce qu'il rend.
"""
from __future__ import annotations

import contextlib
import inspect

import pytest

from oto_mcp.capabilities import procedure_console as PC
from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.orgs import instructions as OI

CTX = ResolvedCtx(sub="u1", org_id=2)


# ── archiver / désarchiver depuis la console ─────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    """Les procédures de l'org 2, avec leur date de retrait éventuelle."""
    etat = {("org", "2", "veille"): {"slug": "veille", "archived_at": None},
            ("org", "2", "ancienne"): {"slug": "ancienne",
                                       "archived_at": "2026-09-01 10:00:00+00"}}
    appels = []

    def _get(ot, oid, slug, version=None):
        row = etat.get((ot, str(oid), slug))
        return dict(row) if row else None

    def _archive(ot, oid, slug):
        appels.append(("archive", slug))
        etat[(ot, str(oid), slug)]["archived_at"] = "2026-10-10 09:00:00+00"
        return True

    monkeypatch.setattr(OI.org_store, "get_instruction", _get)
    monkeypatch.setattr(OI.org_store, "archive_instruction", _archive)
    return etat, appels


def _console(**kw):
    return PC._dispatch_procedure(CTX, PC.ProcedureInput(**kw))


def test_archiver_retire_et_dit_depuis_quand(store):
    _, appels = store
    out = _console(op="archive", slug="veille", scope="org")
    assert appels == [("archive", "veille")]
    assert out["archived"] is True and out["already_archived"] is False
    assert out["archived_at"] == "2026-10-10 09:00:00+00"


def test_re_archiver_ne_touche_a_rien_et_garde_la_date_d_origine(store):
    _, appels = store
    out = _console(op="archive", slug="ancienne", scope="org")
    assert appels == []
    assert out["already_archived"] is True
    assert out["archived_at"] == "2026-09-01 10:00:00+00"


def test_archiver_un_slug_inconnu_est_un_404(store):
    with pytest.raises(AuthzDenied) as e:
        _console(op="archive", slug="fantome", scope="org")
    assert (e.value.status, e.value.code) == (404, "not_found")


def test_desarchiver_passe_par_le_handler_de_la_face_rest(monkeypatch, store):
    monkeypatch.setattr(OI.org_store, "unarchive_instruction",
                        lambda ot, oid, slug: "2026-09-01 10:00:00+00")
    out = _console(op="unarchive", slug="ancienne", scope="org")
    assert out["unarchived"] is True and out["was_archived_at"] == "2026-09-01 10:00:00+00"


def test_archiver_porte_la_garde_de_l_ECRITURE_pas_celle_de_la_suppression():
    """Retirer ne détruit rien : la garde est celle de `set` (un membre au palier
    équipe), pas celle de `delete` (le chef). C'est ce que le signal demandait."""
    source = inspect.getsource(PC)
    assert '"archive": _ECRIRE, "unarchive": _ECRIRE' in source
    assert '"delete": _SUPPRIMER' in source


# ── la liste, retirées comprises sur demande ─────────────────────────────────────

@pytest.fixture
def liste(monkeypatch):
    monkeypatch.setattr(OI.access, "current_group", lambda sub: None)
    monkeypatch.setattr(OI.org_store, "list_instructions",
                        lambda ot, oid, include_base=False: (
                            [{"slug": "veille", "title": "Veille", "description": ""}]
                            if ot == "org" else []))
    monkeypatch.setattr(OI.org_store, "list_archived_instructions",
                        lambda ot, oid: (
                            [{"slug": "ancienne", "title": "Ancienne relance",
                              "description": "", "archived_at": "2026-09-01"}]
                            if ot == "org" else []))


def _slugs(out):
    return [g["slug"] for g in out["guides"]]


def test_la_liste_par_defaut_ne_montre_jamais_une_retiree(liste):
    assert _slugs(_console(op="list")) == ["veille"]


def test_include_archived_ajoute_les_retirees_avec_leur_date(liste):
    out = _console(op="list", include_archived=True)
    assert _slugs(out) == ["veille", "ancienne"]
    [retiree] = [g for g in out["guides"] if g["slug"] == "ancienne"]
    assert retiree["archived_at"] == "2026-09-01" and retiree["scope"] == "org"


def test_include_archived_respecte_la_requete(liste, monkeypatch):
    monkeypatch.setattr(OI.org_store, "search_instructions",
                        lambda ot, oid, q, include_base=False: [])
    assert _slugs(_console(op="list", include_archived=True, query="relance")) == ["ancienne"]
    assert _slugs(_console(op="list", include_archived=True, query="rien")) == []


# ── un agent planifié ou webhook ne déroule pas une procédure retirée ────────────

def test_la_procedure_lue_est_celle_du_proprietaire_d_abord(monkeypatch):
    from oto_mcp import org_store, procedure_retiree
    rows = {("user", "u1"): {"archived_at": None},
            ("org", 2): {"archived_at": "2026-09-01"}}
    monkeypatch.setattr(org_store, "get_instruction",
                        lambda ot, oid, slug: rows.get((ot, oid)))
    # La personnelle est en service : c'est elle que l'agent lit, rien n'est refusé.
    assert procedure_retiree.archivee(2, "u1", "veille") is None
    del rows[("user", "u1")]
    assert procedure_retiree.archivee(2, "u1", "veille") == "2026-09-01"
    assert procedure_retiree.archivee(2, "u1", None) is None


def test_le_tick_n_enfile_rien_et_laisse_une_trace(monkeypatch):
    from oto_mcp import runner_tick
    t = {"id": 7, "org_id": 2, "sub": "u1", "cron": "0 * * * *", "tz": "UTC",
         "next_due": "x", "procedure": "ancienne", "project_id": None}
    ecrit = []
    monkeypatch.setattr(runner_tick.db, "due_triggers", lambda: [t])
    monkeypatch.setattr(runner_tick.db, "consume_due", lambda *a: True)
    monkeypatch.setattr(runner_tick.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_tick.procedure_retiree, "archivee",
                        lambda org, sub, slug: "2026-09-01")
    monkeypatch.setattr(runner_tick.db, "enqueue_job",
                        lambda *a, **k: pytest.fail("rien ne doit être enfilé"))
    monkeypatch.setattr(runner_tick.db, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(runner_tick.db, "enregistrer",
                        lambda conn, tid, org, issue, **k: ecrit.append((tid, issue)))
    assert runner_tick._tick() == 0
    assert ecrit == [(7, "refused_procedure_archived")]


def test_le_webhook_refuse_nommement_et_le_journal_le_dit(monkeypatch):
    from oto_mcp import runner_hook
    t = {"id": 7, "org_id": 2, "sub": "u1", "procedure": "ancienne", "enabled": True}
    ecrit = []
    monkeypatch.setattr(runner_hook, "_authentifier", lambda *a, **k: (t, None, None))
    monkeypatch.setattr(runner_hook.org_suspension, "etat", lambda org: None)
    monkeypatch.setattr(runner_hook.procedure_retiree, "archivee",
                        lambda org, sub, slug: "2026-09-01")
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: contextlib.nullcontext("conn"))
    monkeypatch.setattr(runner_hook.db, "enregistrer",
                        lambda conn, tid, org, issue, **k: ecrit.append((tid, issue)))
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(7, "secret", None)
    assert (e.value.statut, e.value.code) == (409, "procedure_archived")
    assert ecrit == [(7, "refused_procedure_archived")]


# ── lier une page Documents au slot `doc` d'un projet ────────────────────────────

ROW = {"id": 59, "owner_type": "org", "owner_id": "2", "name": "P", "brief_md": "",
       "created_by": "u1", "archived_at": None, "created_at": "x", "updated_at": "x"}


@pytest.fixture
def projet(monkeypatch):
    from oto_mcp.capabilities.docs import common
    vu: dict = {}
    pages = {77: {"id": 77, "project_id": 59}, 88: {"id": 88, "project_id": 60}}
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(ROW))
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: [])
    monkeypatch.setattr(P.db, "log_project_activity", lambda *a, **k: None)
    monkeypatch.setattr(P.db, "get_doc_by_id", lambda i: pages.get(int(i)))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(common, "acces_a_la_page",
                        lambda sub, row: "project" if row["project_id"] == 59 else None)

    def _add(pid, tt, tr, label=None, **k):
        vu.update({"target_type": tt, "target_ref": tr, **k})
        return {"status": "created", "changed": []}
    monkeypatch.setattr(P.db, "add_project_link", _add)
    return vu


def _link(**kw):
    return P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="doc", **kw))


def test_une_page_lisible_se_lie_par_son_id_et_binde_le_slot(projet):
    out = _link(target_ref="77", slot="fiche")
    assert out["link_status"] == "created"
    assert (projet["target_type"], projet["target_ref"], projet["slot"]) == ("doc", "77", "fiche")


def test_une_page_inconnue_est_refusee_nommement(projet):
    with pytest.raises(AuthzDenied) as e:
        _link(target_ref="404")
    assert (e.value.status, e.value.code) == (404, "unknown_doc")


def test_une_page_que_l_auteur_ne_lit_pas_ne_se_lie_pas(projet):
    with pytest.raises(AuthzDenied) as e:
        _link(target_ref="88")
    assert (e.value.status, e.value.code) == (403, "forbidden")


def test_l_audit_compte_le_slot_doc_comme_lie_et_dit_une_page_morte(monkeypatch):
    from oto_mcp import db, project_audit
    monkeypatch.setattr(project_audit, "_resolved_procedure",
                        lambda l: {"slug": "veille", "slots": [{"name": "fiche", "type": "doc"}]})
    monkeypatch.setattr(db, "get_project_by_id", lambda pid: None)
    monkeypatch.setattr(db, "project_run_stats", lambda pid: {}, raising=False)
    monkeypatch.setattr(db, "get_doc_by_id", lambda i: None)
    links = [{"target_type": "procedure", "target_ref": "4"},
             {"target_type": "doc", "target_ref": "77", "slot": "fiche"}]
    audit = project_audit.audit_project(59, links)
    assert audit["unbound_slots"] == []
    assert [d["target_type"] for d in audit["dead_links"]] == ["doc"]


def test_le_boot_ne_purge_plus_les_liens_doc():
    """Rejouée à chaque démarrage, la purge du lot 3 effaçait à chaque déploiement les
    liens que ce type sert de nouveau à poser."""
    from oto_mcp.db import _init
    assert "DELETE FROM project_links WHERE target_type = 'doc'" not in inspect.getsource(_init)
