"""Une seule règle pour une option payante : le droit déclaré (ADR 0070 §7).

`has_option` d'une option PAYANTE (`unipile`) = `has_right(sub, org courante)` : le
droit de l'org, quelle que soit sa source (abonnement, don d'org, partenaire), ou une
ligne de droit posée sur la personne (dans l'org ou partout). **La marque de compte
(`option_comps`) n'ouvre pas d'option payante.** Une option non payante (`beta`) reste
la marque du compte ou de l'org.
"""
from __future__ import annotations

import pytest

from oto_mcp import access


def _wire(monkeypatch, *, droits=(), personne=(), user_comp=False, org_comp=False,
          org=7):
    """`droits` = les (org, droit) d'org vivants dans `org_entitlements` ;
    `personne` = les (org ou None, droit) posés sur la personne `u1`."""
    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None: access.db_entitlements.Posees(
                            1 if (oid, droit) in set(droits) else None,
                            1 if sub == "u1" and ((oid, droit) in set(personne)
                                                  or (None, droit) in set(personne))
                            else None))
    monkeypatch.setattr(access.db, "has_option_comp",
                        lambda et, eid, opt: user_comp if et == "user" else org_comp)
    monkeypatch.setattr(access, "current_org", lambda sub: org)


def test_le_droit_declare_de_l_org_ouvre_l_option_payante(monkeypatch):
    _wire(monkeypatch, droits=[(7, "unipile")])
    assert access.has_option("u1", "unipile") is True


def test_la_marque_de_compte_n_ouvre_pas_l_option_payante(monkeypatch):
    _wire(monkeypatch, user_comp=True)
    assert access.has_option("u1", "unipile") is False


def test_une_ligne_de_droit_de_la_personne_ouvre_l_option_payante(monkeypatch):
    """Dans l'org comme partout : la personne a le droit, son org non."""
    _wire(monkeypatch, personne=[(7, "unipile")])
    assert access.has_option("u1", "unipile") is True
    assert access.has_option("u2", "unipile") is False, "une autre personne non"
    _wire(monkeypatch, personne=[(None, "unipile")], org=None)
    assert access.has_option("u1", "unipile") is True, "sans org : la personne partout"


def test_le_don_d_org_brut_ne_suffit_pas_c_est_le_droit_declare_qui_compte(monkeypatch):
    """`option_comps` n'est plus lu pour une option payante : le droit est posé par
    oto-commerce dans les droits déclarés (#1097)."""
    _wire(monkeypatch, org_comp=True)
    assert access.has_option("u1", "unipile") is False


def test_sans_org_seule_la_personne_partout_est_lue(monkeypatch):
    lus = []
    _wire(monkeypatch, user_comp=True, org=None)
    monkeypatch.setattr(access.db_entitlements, "valeurs_posees",
                        lambda oid, sub, droit, now=None: lus.append(oid)
                        or access.db_entitlements.Posees(None, None))
    assert access.has_option("u1", "unipile") is False, "le défaut d'instance : non"
    assert lus == [None], "sans org, seule la personne partout est interrogée"


def test_l_org_explicite_est_celle_qu_on_lit(monkeypatch):
    # fiche admin d'un tiers : l'org EXPLICITE est utilisée (pas current_org).
    _wire(monkeypatch, droits=[(99, "unipile")])
    monkeypatch.setattr(access, "current_org",
                        lambda sub: (_ for _ in ()).throw(AssertionError("ne doit pas être lu")))
    assert access.has_option("tiers", "unipile", org=99) is True


def test_une_option_non_payante_reste_la_marque_du_compte_ou_de_l_org(monkeypatch):
    _wire(monkeypatch, user_comp=True)
    assert access.has_option("u1", "beta") is True
    _wire(monkeypatch, org_comp=True)
    assert access.has_option("u1", "beta") is True
    _wire(monkeypatch, droits=[(7, "beta")])
    assert access.has_option("u1", "beta") is False, (
        "un drapeau de population n'est pas un droit déclaré de l'org")


def test_user_has_option_ne_regarde_que_le_compte(monkeypatch):
    _wire(monkeypatch, org_comp=True, droits=[(7, "beta")])
    assert access.user_has_option("u1", "beta") is False


def test_le_cockpit_d_org_lit_la_meme_regle(monkeypatch):
    from oto_mcp.capabilities.connectors import activation as cap

    _wire(monkeypatch, droits=[(9, "unipile")], user_comp=True)
    assert cap._org_subscribed(9, "unipile") is True
    assert cap._org_subscribed(8, "unipile") is False


@pytest.mark.parametrize("cle", ["platform_unmetered", "unipile_seats", "members_max",
                                 "platform_key:serper"])
def test_une_cle_du_catalogue_ne_se_lit_jamais_dans_option_comps(monkeypatch, cle):
    """La garde de la coupure (#1097) : hors de la branche payante, une clé du
    catalogue des droits LÈVE au lieu d'être cherchée dans `option_comps` — une marque
    de don héritée, que plus personne ne pose, ne doit rien ouvrir."""
    lus = []
    _wire(monkeypatch, user_comp=True, org_comp=True)
    monkeypatch.setattr(access.db, "has_option_comp",
                        lambda et, eid, opt: lus.append(opt) or True)
    with pytest.raises(ValueError, match="catalogue_key_via_option_comps"):
        access.has_option("u1", cle)
    assert lus == [], "rien n'est lu dans option_comps"
