"""`oto-mcp maintenance apps-lecteurs-actifs` sur une vraie base : les partages d'app
`viewer` par lesquels quelqu'un cliquait.

À blanc, la commande liste ces partages (nominatif, d'équipe, d'org) et n'écrit rien ;
elle écarte qui clique déjà autrement (propriétaire, membre de l'org propriétaire,
partage Use/Edit/Manage), les gestes hors fenêtre et les apps archivées. `--appliquer`
les passe en `contributor` ; rejouée, elle ne trouve plus rien. Elle refuse d'appliquer
tant que la base n'admet pas `contributor`, et n'imprime que des ids et des comptes.
"""
from __future__ import annotations

import uuid

import pytest


def _commande(*args) -> int:
    from oto_mcp import maintenance
    return maintenance.main(["apps-lecteurs-actifs", *args])


def _clic(app_id: int, sub: str, jours_avant: int = 1) -> None:
    from oto_mcp import db
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO app_consents (token_hash, app_id, sub, action, fingerprint, "
            "expires_at, used_at) VALUES (%s, %s, %s, 'write_cell', 'fp', now(), "
            "now() - make_interval(days => %s))",
            (uuid.uuid4().hex, app_id, sub, jours_avant))


def _role(app_id: int, ptype: str, pid) -> str:
    from oto_mcp import db
    with db._connect() as conn:
        return conn.execute(
            "SELECT role FROM resource_grants WHERE resource_type = 'app' "
            "AND resource_id = %s AND principal_type = %s AND principal_id = %s",
            (str(app_id), ptype, str(pid))).fetchone()["role"]


@pytest.fixture
def monde(live):
    from oto_mcp import db, group_store, org_store
    u = uuid.uuid4().hex[:8]
    auteur, perso, membre, editeur, ancien, mute = (
        f"{n}_{u}" for n in ("auteur", "perso", "membre", "editeur", "ancien", "mute"))
    for s in (auteur, perso, membre, editeur, ancien, mute):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    autre = org_store.create_org(f"x_{u}", created_by=auteur)
    org_store.add_org_member(autre, membre, "org_member")
    equipe = group_store.create_group(org, f"g_{u}", created_by=auteur)
    org_store.add_org_member(org, editeur, "org_member")
    group_store.add_group_member(equipe, editeur)

    def app(titre):
        return int(db.apps.create(("user", auteur), org, title=f"{titre} {u}", manifest={},
                                  html="", thumbnail=None, by=auteur)["id"])

    a, b, archivee = app("A"), app("B"), app("C")
    g = lambda rid, pt, pid, role: db.grant_resource(  # noqa: E731
        "app", str(rid), pt, str(pid), role=role, granted_by=auteur)
    g(a, "user", perso, "viewer")          # nominatif : clique → à promouvoir
    g(a, "org", autre, "viewer")           # d'org : son membre clique → à promouvoir
    g(a, "group", equipe, "viewer")        # d'équipe : son membre a aussi Edit → écarté
    g(a, "user", editeur, "editor")
    g(a, "user", ancien, "viewer")         # clic hors fenêtre → écarté
    g(a, "user", mute, "viewer")           # n'a jamais cliqué → écarté
    g(b, "user", perso, "viewer")
    g(archivee, "user", perso, "viewer")   # app archivée → écartée
    _clic(a, perso)
    _clic(a, perso)
    _clic(a, membre)
    _clic(a, editeur)
    _clic(a, auteur)                       # le propriétaire → écarté
    _clic(a, ancien, jours_avant=45)
    _clic(archivee, perso)
    db.apps.set_archived(archivee, True)
    return {"a": a, "b": b, "archivee": archivee, "perso": perso, "autre": autre,
            "equipe": equipe, "ancien": ancien, "mute": mute, "auteur": auteur}


def _mes_lignes(rapport, m):
    return {(p["app_id"], p["principal_type"], p["principal_id"]): (p["personnes"], p["gestes"])
            for p in rapport["partages"] if p["app_id"] in (m["a"], m["b"], m["archivee"])}


def test_a_blanc_liste_les_seuls_lecteurs_qui_cliquaient_et_n_ecrit_rien(monde):
    from oto_mcp import apps_lecteurs_actifs as L
    m = monde
    rapport = L.passer(appliquer=False)
    assert _mes_lignes(rapport, m) == {
        (m["a"], "user", m["perso"]): (1, 2),
        (m["a"], "org", str(m["autre"])): (1, 1),
    }
    assert _role(m["a"], "user", m["perso"]) == "viewer"
    assert L.reste(rapport)
    # La fenêtre s'élargit : le clic d'il y a 45 jours compte.
    large = _mes_lignes(L.passer(appliquer=False, jours=60), m)
    assert (m["a"], "user", m["ancien"]) in large


def test_appliquer_promeut_puis_ne_trouve_plus_rien(monde, capsys):
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    assert not C.peut_cliquer(m["perso"], m["a"])
    assert _commande() == 3
    assert _commande("--appliquer") == 0
    assert _role(m["a"], "user", m["perso"]) == "contributor"
    assert _role(m["a"], "org", m["autre"]) == "contributor"
    assert C.peut_cliquer(m["perso"], m["a"])
    for intact in ((m["a"], "group", m["equipe"]), (m["a"], "user", m["ancien"]),
                   (m["a"], "user", m["mute"]), (m["b"], "user", m["perso"]),
                   (m["archivee"], "user", m["perso"])):
        assert _role(*intact) == "viewer", intact
    sortie = capsys.readouterr().out
    assert m["perso"].split("_")[0] in sortie   # l'id du principal, jamais une adresse
    assert "@" not in sortie
    rapport_rejoue = _commande()
    assert rapport_rejoue in (0, 3)
    from oto_mcp import apps_lecteurs_actifs as L
    assert _mes_lignes(L.passer(appliquer=False), m) == {}


def test_refuse_d_appliquer_sur_une_base_qui_n_admet_pas_contributor(monde, monkeypatch):
    from oto_mcp import apps_lecteurs_actifs as L
    m = monde
    monkeypatch.setattr(L, "_admet_contributor", lambda conn: False)
    assert _commande("--appliquer") == 2
    assert _role(m["a"], "user", m["perso"]) == "viewer"


def test_jours_invalide():
    with pytest.raises(SystemExit):
        _commande("--jours", "0")
