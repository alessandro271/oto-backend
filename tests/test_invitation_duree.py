"""La durée d'une invitation est un réglage d'INSTANCE, en heures, 7 jours par défaut.

`OTO_MCP_INVITATION_TTL_HOURS` (`config.invitation_ttl_hours`) est lu à un seul endroit
par ses trois porteurs : l'invitation (org, équipe, plateforme), le partage en attente
d'un objet, le lien de connexion joint au mail. Ce qui est figé ici :

- le défaut (168 h) et une instance réglée à 24 h, de bout en bout (base, Logto, mail) ;
- une valeur illisible lève, et l'ancien réglage en jours encore défini aussi : aucun
  repli silencieux, et le démarrage refuse (`verifier_duree_invitation`) ;
- le mail DIT la durée réelle, dans les deux langues ;
- sur une vraie base : une invitation échue ne bloque pas la suivante, qui porte un
  jeton neuf pendant que l'ancien n'ouvre plus rien.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from oto_mcp import config, email

ENV = "OTO_MCP_INVITATION_TTL_HOURS"
ANCIEN = "OTO_MCP_INVITE_TTL_DAYS"


@pytest.fixture(autouse=True)
def _reglage_vierge(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv(ANCIEN, raising=False)


@pytest.fixture
def instance_24h(monkeypatch):
    monkeypatch.setenv(ENV, "24")


def test_defaut_sept_jours():
    assert config.invitation_ttl_hours() == 168
    config.verifier_duree_invitation()


def test_une_instance_a_24_h(instance_24h):
    assert config.invitation_ttl_hours() == 24


@pytest.mark.parametrize("brut", ["0", "-3", "sept", "1.5", "24h"])
def test_une_valeur_illisible_leve(monkeypatch, brut):
    monkeypatch.setenv(ENV, brut)
    with pytest.raises(config.DureeInvitationInvalide):
        config.invitation_ttl_hours()
    with pytest.raises(config.DureeInvitationInvalide):
        config.verifier_duree_invitation()


def test_l_ancien_reglage_encore_defini_refuse_le_demarrage(monkeypatch):
    monkeypatch.setenv(ANCIEN, "7")
    with pytest.raises(config.DureeInvitationInvalide, match=ENV):
        config.verifier_duree_invitation()


def _lien_de_connexion(monkeypatch) -> dict:
    """Le corps envoyé à Logto : c'est `expiresIn` qui fait foi."""
    from oto_mcp.auth import annuaire
    import requests
    vus: dict = {}

    class _Rep:
        def raise_for_status(self):
            pass

        def json(self):
            return {"token": "OTT"}

    monkeypatch.setattr(annuaire, "_logto_base", lambda: "https://annuaire.test")
    monkeypatch.setattr(annuaire, "_mgmt_token", lambda: "m2m")
    monkeypatch.setattr(requests, "post", lambda url, json, **kw: vus.update(json) or _Rep())
    assert annuaire.magic_url("https://front.test/invitation/T", "a@b.test").startswith(
        "https://front.test/invitation/T?otl=OTT")
    return vus


def test_le_lien_de_connexion_suit_le_reglage(monkeypatch):
    assert _lien_de_connexion(monkeypatch)["expiresIn"] == 168 * 3600
    monkeypatch.setenv(ENV, "24")
    assert _lien_de_connexion(monkeypatch)["expiresIn"] == 24 * 3600


def test_un_reglage_illisible_n_est_pas_avale_par_le_lien_de_connexion(monkeypatch):
    """Le lien de connexion est au mieux (Logto absent : pas de lien) ; le réglage, lui,
    se lit HORS de ce repli."""
    from oto_mcp.auth import annuaire
    monkeypatch.setenv(ENV, "0")
    with pytest.raises(config.DureeInvitationInvalide):
        annuaire.mint_one_time_token("a@b.test")


def _envoye(monkeypatch) -> dict:
    sent: dict = {}
    monkeypatch.setattr(email, "_send", lambda to, subject, html, **k:
                        sent.update(html=html) or True)
    return sent


@pytest.mark.parametrize("reglage,locale,phrase", [
    (None, "en", "this link is valid for 7 days"),
    (None, None, "ce lien est valable 7 jours"),
    ("24", "en", "this link is valid for 1 day"),
    ("24", None, "ce lien est valable 1 jour."),
    ("12", None, "ce lien est valable 12 heures"),
    ("1", "en", "this link is valid for 1 hour."),
])
def test_le_mail_d_invitation_dit_la_duree_reelle(monkeypatch, reglage, locale, phrase):
    if reglage:
        monkeypatch.setenv(ENV, reglage)
    sent = _envoye(monkeypatch)
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/T", "Alice",
                            brand="oto", locale=locale)
    assert phrase in sent["html"]


def test_le_mail_d_un_partage_en_attente_dit_la_duree_et_lui_seul(monkeypatch, instance_24h):
    sent = _envoye(monkeypatch)
    kw = dict(type_label="projet", name="P", permission="read",
              app_url="https://x/invitation/T", brand="oto")
    email.send_resource_shared_email("a@b.c", lien_d_invitation=True, **kw)
    assert "ce lien est valable 1 jour." in sent["html"]
    email.send_resource_shared_email("a@b.c", **kw)   # partage à un compte existant
    assert "valable" not in sent["html"]


# ── Vrai PostgreSQL ──────────────────────────────────────────────────────────

def _vie(inv_id: int):
    from oto_mcp.db import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT expires_at - created_at AS vie FROM org_invitations WHERE id = %s",
            (inv_id,)).fetchone()["vie"]


def _echoir(inv_id: int) -> None:
    from oto_mcp.db import _connect
    with _connect() as conn:
        conn.execute("UPDATE org_invitations SET expires_at = NOW() - interval '1 minute' "
                     "WHERE id = %s", (inv_id,))


def _environ(vie, heures: int) -> bool:
    return timedelta(hours=heures, minutes=-1) <= vie <= timedelta(hours=heures, minutes=1)


def test_l_invitation_posee_vit_7_jours_par_defaut(live):
    from oto_mcp import org_store
    org = org_store.create_org("inv-duree-defaut", created_by="u-invd-admin")
    inv_id, _ = org_store.create_invitation(org, "defaut@invd.test", "org_member",
                                            "u-invd-admin")
    assert _environ(_vie(inv_id), 168), _vie(inv_id)


def test_l_invitation_et_le_partage_en_attente_vivent_24_h_sur_une_instance_a_24_h(
        live, instance_24h):
    from oto_mcp import org_store, partage_en_attente
    org = org_store.create_org("inv-duree-24h", created_by="u-invd-admin")
    inv_id, _ = org_store.create_invitation(org, "jour@invd.test", "org_member",
                                            "u-invd-admin")
    assert _environ(_vie(inv_id), 24), _vie(inv_id)
    r = partage_en_attente.creer(resource_type="project", kind="project",
                                 resource_id="p-invd-1", email="partage@invd.test",
                                 role="viewer", ttl_days=None, invited_by="u-invd-admin",
                                 nom="P")
    assert _environ(_vie(r["id"]), 24), _vie(r["id"])


def test_une_invitation_echue_se_reemet_avec_un_jeton_neuf(live, monkeypatch):
    """Le geste de l'admin après l'échéance : réinviter la même adresse. Pas de 409
    `already_invited` (l'échue n'est plus en attente), un jeton NEUF, et l'ancien
    n'ouvre plus rien."""
    from types import SimpleNamespace

    from oto_mcp import db, org_store
    from oto_mcp.capabilities.orgs import invites as oi
    monkeypatch.setenv("OTO_INVITE_BASE_URL", "https://front.invd.test")
    monkeypatch.setattr(oi.annuaire, "magic_url", lambda url, email, **_: url)
    monkeypatch.setattr(oi.email_mod, "send_invite_email", lambda *a, **k: True)
    db.upsert_user("u-invd-hote", email="hote@invd.test")
    org = org_store.create_org("inv-reemission", created_by="u-invd-hote")
    org_store.add_org_member(org, "u-invd-hote", "org_admin")
    ctx = SimpleNamespace(sub="u-invd-hote")
    entree = oi.InviteCreateInput(org_id=org, email="neuf@invd.test", send_email=False)

    premier = oi._invite_create(ctx, entree)["invite_url"].rsplit("/", 1)[-1]
    # Encore valide : la seconde émission est un doublon, refusé (#622).
    with pytest.raises(oi.AuthzDenied) as e:
        oi._invite_create(ctx, entree)
    assert e.value.code == "already_invited"

    _echoir(org_store.find_pending_invitation(org, "neuf@invd.test")["id"])
    assert org_store.find_pending_invitation(org, "neuf@invd.test") is None

    second = oi._invite_create(ctx, entree)["invite_url"].rsplit("/", 1)[-1]
    assert second != premier and second.startswith("inv_")
    assert org_store.get_invitation_by_token(premier) is None
    assert org_store.get_invitation_by_token(second) is not None
    db.upsert_user("u-invd-neuf", email="neuf@invd.test")
    assert org_store.accept_invitation(premier, "u-invd-neuf") is None
