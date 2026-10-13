"""Un lien de projet par IDENTIFIANT ne désigne que ce que le projet a le droit de désigner.

Le défaut : `oto_project op=link` résolvait nom et slug dans la portée du projet (#365),
mais stockait un identifiant numérique tel quel, en ne gardant que le droit d'écrire le
PROJET. Le numéro d'un tableau ou d'une procédure d'une AUTRE org, lié à un projet à
soi, était ensuite servi tel quel : libellé et titre dans les liens, lignes et corps sur
la page publiée, lignes recopiées par `op=copy` (mode `seeded`), corps recopié dans
l'org de la copie, schéma posé par un slot.

Sur le montage réel (PostgreSQL) : la pose est refusée, et un lien posé AVANT la garde
(écrit ici directement en base) n'ouvre plus rien chez aucun de ses lecteurs.
"""
from __future__ import annotations

import json
import uuid

import pytest


def _ligne(ns_id: int, nom: str) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                  (ns_id, uuid.uuid4().hex, json.dumps({"nom": nom})))


@pytest.fixture(scope="module")
def monde(live):
    """Moi, dans l'org A ; un tiers, dans l'org V que je ne vois pas. Chacun a un
    tableau garni et une procédure. Une org B partage un tableau à l'org A (le cas
    légitime d'un identifiant hors de l'org)."""
    from oto_mcp import db, org_store

    u = uuid.uuid4().hex[:8]
    moi, tiers = f"moi_{u}", f"tiers_{u}"
    for s in (moi, tiers):
        db.upsert_user(s, email=f"{s}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=moi)
    org_store.add_org_member(org_a, moi)
    org_store.set_active_org(moi, org_a)
    org_v = org_store.create_org(f"v_{u}", created_by=tiers)
    org_store.add_org_member(org_v, tiers)
    org_b = org_store.create_org(f"b_{u}", created_by=tiers)
    org_store.add_org_member(org_b, tiers)

    m = {"moi": moi, "org_a": org_a, "org_v": org_v}
    m["ns_a"] = db.create_datastore("org", str(org_a), "vivier")
    m["ns_v"] = db.create_datastore("org", str(org_v), "clients-secrets")
    _ligne(m["ns_v"], "donnée de V")
    m["ns_b"] = db.create_datastore("org", str(org_b), "partage")
    db.grant_resource("datastore_namespace", str(m["ns_b"]), "org", str(org_a), role="viewer")

    org_store.set_instruction("org", org_a, f"proc-a-{u}", "# A\ncorps de A\n",
                              title="Procédure A", set_by=moi)
    org_store.set_instruction("org", org_v, f"proc-v-{u}", "# V\nCORPS-SECRET-DE-V\n",
                              title="Titre secret de V", set_by=tiers)
    m["proc_a"] = int(org_store.get_instruction("org", org_a, f"proc-a-{u}")["id"])
    m["proc_v"] = int(org_store.get_instruction("org", org_v, f"proc-v-{u}")["id"])

    m["projet"] = db.create_project("org", str(org_a), f"p_{u}", created_by=moi)
    # Le projet piégé : des liens posés AVANT la garde, écrits directement.
    m["piege"] = db.create_project("org", str(org_a), f"piege_{u}", created_by=moi)
    db.add_project_link(m["piege"], "tableau", str(m["ns_v"]), None,
                        config={"provision": "seeded"})
    db.add_project_link(m["piege"], "procedure", str(m["proc_v"]), None)
    db.add_project_link(m["piege"], "tableau", str(m["ns_a"]), "Vivier")
    db.add_project_link(m["piege"], "procedure", str(m["proc_a"]), None)
    return m


def _lit(sub):
    from oto_mcp import ownership
    return lambda t, rid: ownership.can_access(sub, t, rid, "read")


def _link(m, target_type: str, ref) -> dict:
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    return P._project(ResolvedCtx(sub=m["moi"], org_id=m["org_a"]),
                      P.ProjectInput(op="link", project_id=m["projet"],
                                     target_type=target_type, target_ref=str(ref)))


# ── La pose ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target_type,cle,code", [
    ("tableau", "ns_v", "unknown_tableau"), ("procedure", "proc_v", "unknown_procedure")])
def test_lier_le_numero_d_une_autre_org_est_refuse(monde, target_type, cle, code):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _link(monde, target_type, monde[cle])
    assert (e.value.status, e.value.code) == (404, code)
    # Même réponse que pour un numéro qui n'existe pas : rien n'en dit l'existence.
    with pytest.raises(AuthzDenied) as absent:
        _link(monde, target_type, 2_000_000_000)
    assert (absent.value.status, absent.value.code) == (404, code)
    assert str(e.value).replace(str(monde[cle]), "N") == \
        str(absent.value).replace("2000000000", "N")


@pytest.mark.parametrize("target_type,cle", [
    ("tableau", "ns_a"), ("tableau", "ns_b"), ("procedure", "proc_a")])
def test_lier_par_numero_ce_que_le_projet_porte_ou_recoit_passe(monde, target_type, cle):
    """Contrôle positif : l'org du projet, et un partage reçu par elle."""
    out = _link(monde, target_type, monde[cle])
    lien = next(l for l in out["links"] if l["target_ref"] == str(monde[cle]))
    marque = "datastore_id" if target_type == "tableau" else "procedure_id"
    assert lien[marque] == monde[cle]


# ── Les lecteurs d'un lien posé avant la garde ────────────────────────────────

def test_la_liste_ne_resout_pas_un_numero_hors_portee(monde):
    from oto_mcp import db
    liens = {(l["target_type"], l["target_ref"]): l for l in db.list_project_links(monde["piege"])}
    etranger_t = liens[("tableau", str(monde["ns_v"]))]
    etranger_p = liens[("procedure", str(monde["proc_v"]))]
    assert not ({"datastore", "datastore_id"} & set(etranger_t))
    assert not ({"title", "procedure_id"} & set(etranger_p))
    assert liens[("tableau", str(monde["ns_a"]))]["datastore_id"] == monde["ns_a"]
    assert liens[("procedure", str(monde["proc_a"]))]["procedure_id"] == monde["proc_a"]


def _page(monde, chemin: str):
    from oto_mcp import db, share_ui
    projet = {**db.get_project_by_id(monde["piege"]), "mcp_access": "secret",
              "mcp_expose_datastore": True, "mcp_expose_docs": True}
    return share_ui.build_page(projet, chemin, connect_url="https://x/mcp")


def test_la_page_publiee_ne_sert_ni_le_corps_ni_les_lignes_d_ailleurs(monde):
    html, status = _page(monde, f"/procedures/{monde['proc_v']}")
    assert status == 404 and "CORPS-SECRET-DE-V" not in html
    html, status = _page(monde, f"/data/{monde['ns_v']}")
    assert status == 404 and "donnée de V" not in html
    index, _ = _page(monde, "/")
    assert "Titre secret de V" not in index and "clients-secrets" not in index
    # Contrôle positif : ce qui est au projet reste servi.
    assert _page(monde, f"/procedures/{monde['proc_a']}")[1] == 200
    assert _page(monde, f"/data/{monde['ns_a']}")[1] == 200


def test_la_copie_ne_recopie_ni_les_lignes_ni_le_corps_d_ailleurs(monde):
    from oto_mcp import db, org_store
    org_c = org_store.create_org(f"c_{uuid.uuid4().hex[:6]}", created_by=monde["moi"])
    org_store.add_org_member(org_c, monde["moi"])
    new_id, warnings = db.duplicate_project(monde["piege"], "copie", "org", str(org_c),
                                            copied_by=monde["moi"],
                                            lisible=_lit(monde["moi"]))
    liens = db.list_project_links(new_id)
    assert not any(l["target_ref"] in (str(monde["ns_v"]), str(monde["proc_v"]))
                   for l in liens)
    for l in liens:
        if l["target_type"] == "tableau" and l.get("datastore_id"):
            assert all(r["data"].get("nom") != "donnée de V"
                       for r in db.datastore_list_rows(l["datastore_id"], limit=None))
    # La copie d'une procédure garde son titre : aucune n'est arrivée de V.
    titres = [i.get("title") for i in org_store.list_instructions("org", org_c)]
    assert "Titre secret de V" not in titres and "Procédure A" in titres
    assert len([w for w in warnings if "ne résout plus" in w]) == 2


def test_le_slot_n_ecrit_pas_le_schema_d_un_tableau_d_ailleurs(monde):
    from oto_mcp import db, slots
    cible = {"fields": [{"key": "x", "type": "text"}]}
    res = slots.provision_tableau_schema(monde["ns_v"], cible, project_id=monde["piege"])
    assert res == {"status": "out_of_scope"}
    assert db.get_datastore_by_id(monde["ns_v"])["schema"] in (None, {})


# ── Le projet d'un agent (runner_triggers) ────────────────────────────────────

def test_un_agent_ne_porte_qu_un_projet_de_son_org(monde):
    from oto_mcp import db
    from oto_mcp.capabilities import runner_triggers as R
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    ctx = ResolvedCtx(sub=monde["moi"], org_id=monde["org_a"])
    ailleurs = db.create_project("org", str(monde["org_v"]), "projet de V",
                                 created_by="x")
    for pid in (ailleurs, 2_000_000_000):
        with pytest.raises(AuthzDenied) as e:
            R._exiger_projet_de_l_org(ctx, pid)
        assert (e.value.status, e.value.code) == (404, "unknown_project")
    R._exiger_projet_de_l_org(ctx, monde["projet"])
    R._exiger_projet_de_l_org(ctx, None)


# ── Les projets qui ne sont pas d'org : la portée ne doit rien perdre de légitime ─

def test_les_liens_legitimes_d_un_projet_d_equipe_ou_perso_restent_vivants(monde):
    """Équipe → tableau de l'org parente ; perso rangé dans l'org → tableau de l'org ;
    perso sans org de création (legacy, liste d'orgs VIDE) → mon tableau perso ; projet
    d'org → MA procédure personnelle (qui ne se range dans aucune org)."""
    from oto_mcp import db, group_store, org_store
    u = uuid.uuid4().hex[:6]
    moi, org_a = monde["moi"], monde["org_a"]
    equipe = group_store.create_group(org_a, f"g_{u}")
    group_store.add_group_member(equipe, moi)
    ns_perso = db.create_datastore("user", moi, f"perso_{u}")
    org_store.set_instruction("user", moi, f"perso-{u}", "# moi\n", title="Perso", set_by=moi)
    proc_perso = int(org_store.get_instruction("user", moi, f"perso-{u}")["id"])

    cas = [
        (db.create_project("group", str(equipe), f"eq_{u}", created_by=moi),
         "tableau", monde["ns_a"]),
        (db.create_project("user", moi, f"pc_{u}", created_by=moi, context_org_id=org_a),
         "tableau", monde["ns_a"]),
        (db.create_project("user", moi, f"pl_{u}", created_by=moi),
         "tableau", ns_perso),
        (monde["projet"], "procedure", proc_perso),
    ]
    for pid, tt, ref in cas:
        assert db.refs_dans_la_portee(pid, tt, [ref]) == {ref}, (pid, tt)
        db.add_project_link(pid, tt, str(ref), None)
        lien = next(l for l in db.list_project_links(pid) if l["target_ref"] == str(ref))
        assert lien["datastore_id" if tt == "tableau" else "procedure_id"] == ref
    # Et le tableau de V reste hors de chacune de ces portées.
    for pid, _, _ in cas:
        assert db.refs_dans_la_portee(pid, "tableau", [monde["ns_v"]]) == set()


# ── Revue du 12/10 : le lieur doit LIRE la cible, même quand elle est au propriétaire ─

@pytest.fixture(scope="module")
def externe(monde):
    """Un compte EXTÉRIEUR à l'org A, bénéficiaire d'un partage en ÉCRITURE du projet."""
    from oto_mcp import db
    sub = f"ext_{uuid.uuid4().hex[:8]}"
    db.upsert_user(sub, email=f"{sub}@example.test")
    db.grant_resource("project", str(monde["projet"]), "user", sub, role="editor")
    db.grant_resource("project", str(monde["piege"]), "user", sub, role="viewer")
    return sub


def _link_par(m, sub, target_type, ref, **kw) -> dict:
    from oto_mcp.capabilities import projects as P
    from oto_mcp.capabilities._types import ResolvedCtx
    return P._project(ResolvedCtx(sub=sub, org_id=m["org_a"]),
                      P.ProjectInput(op="link", project_id=m["projet"],
                                     target_type=target_type, target_ref=str(ref), **kw))


def test_un_beneficiaire_exterieur_ne_lie_pas_un_tableau_de_l_org(monde, externe):
    """B1 : la cible est à l'org propriétaire du projet, donc DANS sa portée — mais le
    lieur n'est pas membre et ne la lit pas. Un membre, lui, la lie."""
    from oto_mcp import ownership
    from oto_mcp.capabilities._types import AuthzDenied
    assert ownership.can_access(externe, "project", str(monde["projet"]), "write")
    with pytest.raises(AuthzDenied) as e:
        _link_par(monde, externe, "tableau", monde["ns_a"])
    assert (e.value.status, e.value.code) == (404, "unknown_tableau")
    with pytest.raises(AuthzDenied) as e:
        _link_par(monde, externe, "procedure", monde["proc_a"])
    assert (e.value.status, e.value.code) == (404, "unknown_procedure")
    out = _link_par(monde, monde["moi"], "tableau", monde["ns_a"])
    assert any(l.get("datastore_id") == monde["ns_a"] for l in out["links"])


def test_la_copie_ecarte_ce_que_le_copieur_ne_lit_pas(monde, externe):
    """F-063 : le lien `seeded` vers le tableau de l'org A est DANS la portée du projet
    source ; un lecteur extérieur qui copie ne reçoit pourtant ni ses lignes, ni la
    procédure de A. La copie aboutit, le lien est écarté et le dit."""
    from oto_mcp import db, org_store
    _ligne(monde["ns_a"], "donnée de A")
    org_x = org_store.create_org(f"x_{uuid.uuid4().hex[:6]}", created_by=externe)
    org_store.add_org_member(org_x, externe)
    src = db.create_project("org", str(monde["org_a"]), "seme", created_by=monde["moi"])
    db.add_project_link(src, "tableau", str(monde["ns_a"]), "Vivier",
                        config={"provision": "seeded"})
    db.add_project_link(src, "procedure", str(monde["proc_a"]), None)
    db.grant_resource("project", str(src), "user", externe, role="viewer")
    new_id, warnings = db.duplicate_project(src, "copie", "org", str(org_x),
                                            copied_by=externe, lisible=_lit(externe))
    assert db.list_project_links(new_id) == []
    assert len([w for w in warnings if "illisible pour toi" in w]) == 2
    assert "Procédure A" not in [i.get("title")
                                 for i in org_store.list_instructions("org", org_x)]
    # Contrôle positif : le membre, lui, reçoit le vivier semé.
    org_y = org_store.create_org(f"y_{uuid.uuid4().hex[:6]}", created_by=monde["moi"])
    org_store.add_org_member(org_y, monde["moi"])
    new_id, _ = db.duplicate_project(src, "copie", "org", str(org_y),
                                     copied_by=monde["moi"], lisible=_lit(monde["moi"]))
    seme = next(l for l in db.list_project_links(new_id) if l["target_type"] == "tableau")
    assert [r["data"]["nom"] for r in db.datastore_list_rows(seme["datastore_id"],
                                                             limit=None)] == ["donnée de A"]


def test_un_lien_hors_portee_le_dit_a_l_audit(monde):
    """N3 : un lien posé avant la garde vers une cible qui EXISTE ailleurs n'est pas
    « supprimé » — il est hors portée, et le remède est de le retirer."""
    from oto_mcp import db, project_audit
    db.add_project_link(monde["piege"], "tableau", "2000000001", None)   # n'existe pas
    liens = db.list_project_links(monde["piege"])
    par_ref = {l["target_ref"]: l for l in liens}
    assert par_ref[str(monde["ns_v"])].get("hors_portee") is True
    assert par_ref[str(monde["proc_v"])].get("hors_portee") is True
    assert "hors_portee" not in par_ref["2000000001"]
    assert "hors_portee" not in par_ref[str(monde["ns_a"])]
    morts = {d["target_ref"]: d["why"]
             for d in project_audit.audit_project(monde["piege"], liens)["dead_links"]}
    assert morts[str(monde["ns_v"])].startswith("hors de la portée du projet")
    assert morts[str(monde["proc_v"])].startswith("hors de la portée du projet")
    assert morts["2000000001"] == "le tableau pointé n'existe plus"


def test_la_procedure_d_une_equipe_se_lie_par_ses_membres_seulement(monde):
    """N9 : procédure d'ÉQUIPE de l'org A, projet de l'org A. Dans la portée ; le
    membre de l'équipe la lie, un membre de l'org hors de l'équipe non."""
    from oto_mcp import db, group_store, org_store
    from oto_mcp.capabilities._types import AuthzDenied
    u = uuid.uuid4().hex[:6]
    collegue = f"col_{u}"
    db.upsert_user(collegue, email=f"{collegue}@example.test")
    org_store.add_org_member(monde["org_a"], collegue)
    equipe = group_store.create_group(monde["org_a"], f"t_{u}")
    group_store.add_group_member(equipe, collegue)
    org_store.set_instruction("group", equipe, f"eq-{u}", "# eq\n", title="Eq",
                              set_by=collegue)
    proc = int(org_store.get_instruction("group", equipe, f"eq-{u}")["id"])
    with pytest.raises(AuthzDenied) as e:
        _link_par(monde, monde["moi"], "procedure", proc)   # moi : pas dans l'équipe
    assert e.value.status == 404
    db.grant_resource("project", str(monde["projet"]), "user", collegue, role="editor")
    out = _link_par(monde, collegue, "procedure", proc)
    assert any(l.get("procedure_id") == proc for l in out["links"])


def test_un_tableau_de_la_bibliotheque_plateforme_se_lie(monde):
    """N9 : un tableau PLATEFORME est lisible par tous, et dans toute portée."""
    from oto_mcp import db
    try:
        ns = db.create_datastore("platform", "platform", f"biblio_{uuid.uuid4().hex[:6]}")
    except Exception as e:  # noqa: BLE001 — la base refuse ce propriétaire : rien à lier
        pytest.skip(f"pas de tableau plateforme créable ici : {e}")
    out = _link(monde, "tableau", ns)
    assert any(l.get("datastore_id") == ns for l in out["links"])


def test_le_slot_par_le_vrai_chemin_ne_lie_ni_ne_schematise_un_tableau_d_ailleurs(monde):
    """N9 : `op=link … slot=` sur le tableau de V — refus 404 avant toute écriture,
    schéma de V intact, aucun lien posé."""
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _link_par(monde, monde["moi"], "tableau", monde["ns_v"], slot="vivier")
    assert (e.value.status, e.value.code) == (404, "unknown_tableau")
    assert db.get_datastore_by_id(monde["ns_v"])["schema"] in (None, {})
    assert not any(l.get("slot") == "vivier" for l in db.list_project_links(monde["projet"]))


def test_une_cible_lisible_d_une_autre_org_est_refusee_en_disant_quoi_faire(monde):
    """Membre de deux orgs : lier le tableau de l'autre org reste refusé (un lien le
    montrerait à tout le projet), mais le refus le dit et propose de copier ou de
    partager — il ne fait pas croire que le tableau n'existe pas."""
    from oto_mcp import db, org_store
    from oto_mcp.capabilities._types import AuthzDenied
    org_c = org_store.create_org(f"c_{uuid.uuid4().hex[:8]}", created_by=monde["moi"])
    org_store.add_org_member(org_c, monde["moi"])
    ns_c = db.create_datastore("org", str(org_c), "a-moi-ailleurs")
    with pytest.raises(AuthzDenied) as e:
        _link(monde, "tableau", ns_c)
    assert (e.value.status, e.value.code) == (403, "target_out_of_scope")
    assert "Copie" in str(e.value) and "partage" in str(e.value)
