"""`oto-mcp maintenance orphelins` : retirer les références à un connecteur absent du
catalogue servi (#905).

À blanc par défaut ; `--appliquer` supprime les installations, retraits et activations
orphelins et retire les noms orphelins des kits d'org, sans toucher aux autres noms ;
jamais les credentials ni leurs instances sans `--credentials`, lui-même refusé tant
qu'une date de préavis échue n'est pas donnée. Idempotent, relu après coup.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from oto_mcp import maintenance
from oto_mcp.connectors import orphelins, orphelins_nettoyage as on

CONNU = "serper"
AUTRE = "hunter"
AUJOURD_HUI = date(2026, 10, 14)


def _seme(fantome: str) -> int:
    """Une org dont chaque table nomme `fantome` ET un connecteur connu ; une seconde
    org dont le kit ne nomme que du connu. Rend l'id de la première."""
    from oto_mcp import db
    with db._connect() as conn:
        org = conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                           (f"banc-{fantome}",)).fetchone()["id"]
        conn.execute("UPDATE orgs SET default_connectors = %s WHERE id = %s",
                     ([CONNU, fantome, AUTRE], org))
        conn.execute("INSERT INTO orgs (name, default_connectors) VALUES (%s, %s)",
                     (f"banc-{fantome}-saine", [AUTRE, CONNU]))
        for nom in (CONNU, fantome):
            conn.execute("INSERT INTO user_selected_connectors (sub, org_id, connector) "
                         "VALUES ('sub-a', %s, %s), ('sub-b', %s, %s)", (org, nom, org, nom))
            conn.execute("INSERT INTO connector_selection_removed (sub, org_id, connector) "
                         "VALUES ('sub-c', %s, %s)", (org, nom))
            conn.execute("INSERT INTO connector_availability (scope_type, scope_id, "
                         "connector, enabled) VALUES ('org', %s, %s, true)", (str(org), nom))
            conn.execute("INSERT INTO connector_credentials (entity_type, entity_id, "
                         "connector, secret_enc) VALUES ('org', %s, %s, 'x')", (str(org), nom))
            conn.execute(f"INSERT INTO {orphelins.INSTANCES} (connector, owner_type, owner_id) "
                         "VALUES (%s, 'org', %s)", (nom, str(org)))
        # Un pont remote se définit par sa donnée : jamais un orphelin, jamais retiré.
        conn.execute("INSERT INTO connector_credentials (entity_type, entity_id, connector, "
                     "secret_enc, meta) VALUES ('org', %s, %s, 'x', %s)",
                     (str(org), f"{fantome}_pont", '{"base_url": "https://pont.exemple.test"}'))
    return org


def _compte(table: str, nom: str) -> int:
    from oto_mcp import db
    with db._connect() as conn:
        lignes = conn.execute(dict(orphelins.REFERENCES)[table],
                              {"catalogue": sorted(orphelins.catalogue())}).fetchall()
    return sum(int(r["lignes"]) for r in lignes if r["connecteur"] == nom)


def _kit(org: int) -> list[str]:
    from oto_mcp import db
    with db._connect() as conn:
        return conn.execute("SELECT default_connectors FROM orgs WHERE id = %s",
                            (org,)).fetchone()["default_connectors"]


def _par_table(rapport: dict, cle: str, nom: str) -> dict[str, int]:
    return {t["table"]: t[cle].get(nom, 0) for t in rapport["tables"]}


def _lues(rapport: dict, nom: str) -> dict[str, int]:
    return {t["table"]: t["lignes"].get(nom, 0) for t in rapport["intouchees"]}


# ── le refus des credentials : avant toute lecture ───────────────────────────

@pytest.mark.parametrize("preavis", [None, AUJOURD_HUI, AUJOURD_HUI + timedelta(days=3)])
def test_credentials_refuses_sans_preavis_echu(monkeypatch, preavis):
    monkeypatch.setattr(on, "_traiter", lambda *_a: pytest.fail("rien ne doit être lu"))
    with pytest.raises(on.PreavisNonEchu):
        on.nettoyer(appliquer=True, credentials=True, preavis_echu=preavis,
                    aujourd_hui=AUJOURD_HUI)


def test_la_commande_rend_le_code_du_refus(monkeypatch, capsys):
    monkeypatch.setattr(on, "_traiter", lambda *_a: pytest.fail("rien ne doit être lu"))
    assert maintenance.main(["orphelins", "--appliquer", "--credentials"]) == \
        maintenance.ORPHELINS_REFUS
    assert "REFUS" in capsys.readouterr().err
    demain = (date.today() + timedelta(days=1)).isoformat()
    assert maintenance.main(["orphelins", "--credentials", "--preavis-echu", demain]) == \
        maintenance.ORPHELINS_REFUS


def test_un_preavis_sans_credentials_est_une_erreur_d_usage():
    with pytest.raises(SystemExit) as e:
        maintenance.main(["orphelins", "--preavis-echu", "2026-01-01"])
    assert e.value.code == 2


def test_le_catalogue_est_le_registre_servi_et_les_tables_celles_du_constat():
    from oto_mcp import providers
    assert orphelins.catalogue() == frozenset(providers.REGISTRY)
    traitees = {*on.SUPPRIMEES, orphelins.KIT, orphelins.CREDENTIALS}
    assert traitees <= {t for t, _ in orphelins.REFERENCES}


# ── sur la base ────────────────────────────────────────────────────────────

def test_a_blanc_rien_n_est_ecrit(live, capsys):
    fantome = "disparu_a_blanc_905"
    org = _seme(fantome)
    rapport = on.nettoyer(appliquer=False)
    assert rapport["applique"] is False
    assert _par_table(rapport, "a_retirer", fantome) == {
        "user_selected_connectors": 2, "connector_selection_removed": 1,
        "connector_availability": 1, orphelins.KIT: 1}
    for table in (*on.SUPPRIMEES, orphelins.KIT, orphelins.CREDENTIALS, orphelins.INSTANCES):
        assert _compte(table, fantome) >= 1, table
    assert _kit(org) == [CONNU, fantome, AUTRE]
    # La commande, à blanc par défaut, dit ce qu'elle retirerait.
    assert maintenance.main(["orphelins"]) == maintenance.ORPHELINS_FAIT
    sortie = capsys.readouterr().out
    assert "À BLANC" in sortie and f"{fantome} 2" in sortie
    assert _compte("user_selected_connectors", fantome) == 2


def test_appliquer_retire_relit_et_garde_credentials_et_instances(live):
    fantome = "disparu_applique_905"
    org = _seme(fantome)
    rapport = on.nettoyer(appliquer=True)
    assert _par_table(rapport, "retire", fantome) == {
        "user_selected_connectors": 2, "connector_selection_removed": 1,
        "connector_availability": 1, orphelins.KIT: 1}
    assert not on.reste(rapport)
    for table in (*on.SUPPRIMEES, orphelins.KIT):
        assert _compte(table, fantome) == 0, table
    # Le connu reste partout.
    from oto_mcp import db
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM user_selected_connectors "
                            "WHERE org_id = %s AND connector = %s",
                            (org, CONNU)).fetchone()["n"] == 2
    # Credentials et instances : intouchés sans l'option, lus et rapportés.
    lues = _lues(rapport, fantome)
    assert lues[orphelins.CREDENTIALS] == 1 and lues[orphelins.INSTANCES] == 1
    assert _compte(orphelins.CREDENTIALS, fantome) == 1
    assert _compte(orphelins.INSTANCES, fantome) == 1


def test_le_kit_perd_l_orphelin_et_garde_ses_autres_noms_dans_l_ordre(live):
    fantome = "disparu_kit_905"
    org = _seme(fantome)
    from oto_mcp import db
    with db._connect() as conn:
        saine = conn.execute("SELECT id FROM orgs WHERE name = %s",
                             (f"banc-{fantome}-saine",)).fetchone()["id"]
        sans_kit = conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                                (f"banc-{fantome}-sans-kit",)).fetchone()["id"]
    on.nettoyer(appliquer=True)
    assert _kit(org) == [CONNU, AUTRE]
    assert _kit(saine) == [AUTRE, CONNU]
    assert _kit(sans_kit) is None


def test_idempotent(live, capsys):
    fantome = "disparu_relance_905"
    _seme(fantome)
    on.nettoyer(appliquer=True)
    second = on.nettoyer(appliquer=True)
    assert all(t["retire"] == {} and t["reste"] == {} for t in second["tables"])
    assert maintenance.main(["orphelins", "--appliquer"]) == maintenance.ORPHELINS_FAIT
    assert "APPLIQUÉ" in capsys.readouterr().out


def test_credentials_apres_preavis_echu_passent_par_le_coffre(live):
    fantome = "disparu_cle_905"
    org = _seme(fantome)
    rapport = on.nettoyer(appliquer=True, credentials=True,
                          preavis_echu=AUJOURD_HUI - timedelta(days=1),
                          aujourd_hui=AUJOURD_HUI)
    (cles,) = [t for t in rapport["tables"] if t["table"] == orphelins.CREDENTIALS]
    assert cles["retire"].get(fantome) == 1 and cles["reste"] == {}
    assert _compte(orphelins.CREDENTIALS, fantome) == 0
    # L'instance est ARCHIVÉE par l'entonnoir du coffre, jamais supprimée.
    assert _lues(rapport, fantome)[orphelins.INSTANCES] == 0
    from oto_mcp import db
    with db._connect() as conn:
        archivee = conn.execute(
            f"SELECT revoked_at FROM {orphelins.INSTANCES} WHERE owner_type = 'org' "
            "AND owner_id = %s AND connector = %s", (str(org), fantome)).fetchone()
        assert archivee is not None and archivee["revoked_at"] is not None
        # Le pont remote et la clé du connecteur connu restent.
        restants = {r["connector"] for r in conn.execute(
            "SELECT connector FROM connector_credentials WHERE entity_type = 'org' "
            "AND entity_id = %s", (str(org),)).fetchall()}
    assert restants == {CONNU, f"{fantome}_pont"}
