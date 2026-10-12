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
                                            copied_by=monde["moi"])
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
