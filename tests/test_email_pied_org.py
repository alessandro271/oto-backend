"""Le pied de page d'un `email_send` fait avec la clé de l'org — trois cas, pas deux.

Décision d'Alexis du 12/09/2026, verbatim : « Autoriser, désabonnement exigé — l'org
peut retirer notre pied de page à condition de fournir le sien ; sinon le nôtre
reste. » Le constat qui l'a amenée : apporter sa clé changeait le TRANSPORT, pas le
GABARIT — un prospect froid lisait « vous avez un compte oto » et se voyait proposer
de se désabonner auprès de nous (oto-backend#443).

1. **clé de l'org + désabonnement déclaré** → le pied de l'org part, le nôtre non ;
2. **clé de l'org sans désabonnement** → le nôtre reste, et la demande de retrait sans
   moyen de se désabonner est refusée par un message qui NOMME le geste ;
3. **clé commune** → le nôtre, toujours, même si l'org a déclaré un désabonnement.

La déclaration et la route se jouent sur PostgreSQL réel : la preuve porte sur ce que
lit `email_send`, pas sur un stub de ce qu'il devrait lire. Chaque cas a été éprouvé
en chute — correctif retiré, rouge constaté, correctif remis.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

from oto_mcp import email as E

_CORPS = "bonjour,\n\nun mot pour vous présenter notre offre."
_LIEN = "https://org.exemple.test/desabonnement"
_ADRESSE = "desinscription@org.exemple.test"
_NOTRE_MENTION = "vous avez un compte oto"
_NOTRE_SIGNATURE = "oto · oto.cx"
_ADMIN = "logto:admin-pied"


# ── le gabarit ───────────────────────────────────────────────────────────────

def test_le_pied_de_l_org_remplace_le_notre_sans_signer_de_notre_nom():
    html = E.render_composed_email(_CORPS, org_footer={"unsubscribe_url": _LIEN})
    assert f'href="{_LIEN}"' in html and "se désabonner</a>" in html
    assert "pour ne plus recevoir nos messages" in html
    assert _NOTRE_MENTION not in html and _NOTRE_SIGNATURE not in html


def test_une_adresse_seule_suffit_et_se_lit_dans_le_pied():
    html = E.render_composed_email(_CORPS, org_footer={"unsubscribe_email": _ADRESSE})
    assert f"écrivez à {_ADRESSE}." in html
    assert _NOTRE_MENTION not in html and _NOTRE_SIGNATURE not in html


def test_sans_pied_d_org_le_rendu_est_celui_d_avant_a_l_octet():
    assert E.render_composed_email(_CORPS, org_footer=None) == E.render_composed_email(_CORPS)


def test_le_gabarit_ne_rend_jamais_un_pied_d_org_sans_desabonnement():
    with pytest.raises(ValueError, match="unsubscribe"):
        E.render_composed_email(_CORPS, org_footer={"unsubscribe_url": "  "})


def test_la_cle_commune_n_accepte_pas_de_pied_d_org():
    """Le mailer part avec la clé commune : il n'a pas de porte pour le pied d'une org."""
    import inspect
    assert "org_footer" not in inspect.signature(E.send_composed_email).parameters


# ── le monde réel : une org, ses réglages, l'outil ───────────────────────────

@pytest.fixture(scope="module")
def live(pg_module_dsn):
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


def _org_expeditrice(connecteur: str = "resend") -> int:
    from oto_mcp import org_store
    from oto_mcp.capabilities.orgs import email_settings as cap
    oid = org_store.create_org(f"pied-{uuid.uuid4().hex[:8]}", created_by=_ADMIN)
    org_store.add_org_member(oid, _ADMIN, "org_admin")
    cap._set_email_settings(SimpleNamespace(sub=_ADMIN), cap.SetEmailSettingsInput(
        org_id=oid, connector=connecteur,
        senders=[{"email": "hello@org.exemple.test", "name": "Org"}]))
    return oid


def _declarer(oid: int, connecteur: str = "resend", **kw) -> dict:
    """Par la face servie à l'agent : `oto_org_settings domain=email op=set`."""
    from oto_mcp.capabilities import org_console as C
    return C._org_settings(SimpleNamespace(sub=_ADMIN), C.OrgSettingsInput(
        op="set", domain="email", org_id=oid, connector=connecteur, **kw))


@pytest.fixture
def outil(monkeypatch):
    from fastmcp import FastMCP
    from oto_mcp.tools import email as T
    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool("email_send"))


def _en_membre(monkeypatch, oid, super_admin=False):
    from oto_mcp.tools import email as T
    monkeypatch.setattr(T, "_sub_or_raise", lambda: _ADMIN)
    monkeypatch.setattr(T.access, "current_org", lambda sub: oid)
    monkeypatch.setattr(T.access, "is_super_admin", lambda sub: super_admin)
    monkeypatch.setattr(T.roles, "is_org_member", lambda sub, org: True)
    monkeypatch.setattr(T.config, "front_for", lambda sub: (None, None))


def _envoyer(outil, **kw):
    base = dict(ctx=None, to="prospect@ailleurs.test", subject="objet", body=_CORPS)
    base.update(kw)
    return outil.fn(**base)


# ── cas 1 : clé de l'org + désabonnement déclaré → le pied de l'org ──────────

@pytest.mark.parametrize("connecteur, transport", [("resend", "resend"), ("scaleway", "scaleway")])
def test_cle_propre_et_desabonnement_declare_le_pied_de_l_org_part(
        live, outil, monkeypatch, connecteur, transport):
    oid = _org_expeditrice(connecteur)
    assert _declarer(oid, connecteur, footer={"unsubscribe_url": _LIEN})["footer"] == \
        {"unsubscribe_url": _LIEN}
    _en_membre(monkeypatch, oid)
    out = _envoyer(outil, dry_run=True)
    assert out["transport"] == transport and out["footer"] == "org"
    assert f'href="{_LIEN}"' in out["html"]
    assert _NOTRE_MENTION not in out["html"] and _NOTRE_SIGNATURE not in out["html"]


def test_le_pied_de_l_org_part_aussi_en_envoi_immediat_et_differe(live, outil, monkeypatch):
    from oto_mcp.tools import email as T
    oid = _org_expeditrice("resend")
    _declarer(oid, footer={"unsubscribe_email": _ADRESSE})
    _en_membre(monkeypatch, oid)

    parti = {}
    monkeypatch.setattr(T.access, "resolve_api_key", lambda p: ("re_cle_org", False))
    monkeypatch.setattr(T.mailer, "send_via_resend",
                        lambda to, subject, html, **kw: parti.update(html=html) or True)
    assert _envoyer(outil, force_now=True)["footer"] == "org"
    assert f"écrivez à {_ADRESSE}" in parti["html"] and _NOTRE_MENTION not in parti["html"]

    file_ = {}
    monkeypatch.setattr(T.org_store, "has_org_secret", lambda org, p: True)
    monkeypatch.setattr(T.db, "enqueue_scheduled_email", lambda **kw: file_.update(kw) or 9)
    out = _envoyer(outil, send_at="2999-01-01T09:00")
    assert out["scheduled"] is True and out["footer"] == "org"
    assert f"écrivez à {_ADRESSE}" in file_["body_html"]
    assert _NOTRE_MENTION not in file_["body_html"]


# ── cas 2 : clé de l'org sans désabonnement → le nôtre, et le refus nommé ────

def test_cle_propre_sans_desabonnement_notre_pied_reste(live, outil, monkeypatch):
    oid = _org_expeditrice("resend")
    _en_membre(monkeypatch, oid)
    out = _envoyer(outil, dry_run=True)
    assert out["transport"] == "resend" and out["footer"] == "platform"
    assert _NOTRE_MENTION in out["html"] and _NOTRE_SIGNATURE in out["html"]


@pytest.mark.parametrize("demande", [
    {},
    {"unsubscribe_url": "", "unsubscribe_email": "  "},
    {"url": _LIEN},              # la clé mal nommée n'est pas un désabonnement
])
def test_demander_le_retrait_sans_desabonnement_est_refuse_en_nommant_le_geste(
        live, outil, monkeypatch, demande):
    from oto_mcp import org_store
    from oto_mcp.capabilities._types import AuthzDenied
    oid = _org_expeditrice("resend")
    with pytest.raises(AuthzDenied) as e:
        _declarer(oid, footer=demande,
                  senders=[{"email": "autre@org.exemple.test"}])
    assert e.value.status == 400 and e.value.code == "unsubscribe_required"
    msg = e.value.message
    assert "désabonnement" in msg and "unsubscribe_url" in msg and "unsubscribe_email" in msg
    assert "reste" in msg, "le refus dit aussi ce qui se passe sans le geste"
    # Rien n'est écrit — ni le pied, ni les expéditeurs passés dans le même appel.
    assert org_store.org_email_footer(oid, "resend") is None
    assert org_store.resolve_sender(oid)[0]["email"] == "hello@org.exemple.test"
    _en_membre(monkeypatch, oid)
    assert _envoyer(outil, dry_run=True)["footer"] == "platform"


def test_retirer_le_pied_de_l_org_rend_le_notre(live, outil, monkeypatch):
    oid = _org_expeditrice("resend")
    _declarer(oid, footer={"unsubscribe_url": _LIEN})
    assert _declarer(oid, clear_footer=True)["footer"] is None
    _en_membre(monkeypatch, oid)
    out = _envoyer(outil, dry_run=True)
    assert out["footer"] == "platform" and _NOTRE_MENTION in out["html"]


def test_le_desabonnement_vaut_pour_son_connecteur_seul(live, outil, monkeypatch):
    oid = _org_expeditrice("resend")
    _declarer(oid, "scaleway", footer={"unsubscribe_url": _LIEN})
    _en_membre(monkeypatch, oid)
    out = _envoyer(outil, dry_run=True)
    assert out["transport"] == "resend" and out["footer"] == "platform"
    assert _LIEN not in out["html"]


# ── cas 3 : clé commune → le nôtre, toujours ─────────────────────────────────

def test_cle_commune_notre_pied_reste_meme_si_l_org_a_declare_le_sien(live, outil, monkeypatch):
    """Repli marque (super_admin, aucune adresse d'org) : l'org a pourtant un
    désabonnement déclaré sur un connecteur."""
    from oto_mcp import org_store
    oid = org_store.create_org(f"pied-{uuid.uuid4().hex[:8]}", created_by=_ADMIN)
    _declarer(oid, "resend", footer={"unsubscribe_url": _LIEN})
    _en_membre(monkeypatch, oid, super_admin=True)
    out = _envoyer(outil, dry_run=True)
    assert out["transport"] == "mailer" and out["footer"] == "platform"
    assert _NOTRE_MENTION in out["html"] and _LIEN not in out["html"]


def test_la_condition_de_cle_vit_dans_l_outil_pas_seulement_dans_la_route(outil, monkeypatch):
    """Une route de clé commune qui porterait un pied d'org (défaut futur de la route)
    ne le fait pas partir : c'est `email_send` qui décide, sur la clé."""
    from oto_mcp.tools import email as T
    route = {"org_id": None, "connector": None, "from_email": None, "from_name": None,
             "transport": "mailer", "reply_to": None, "quiet_hours": None,
             "footer": {"unsubscribe_url": _LIEN}}
    monkeypatch.setattr(T, "_resolve_route", lambda from_email: ("u1", route))
    out = _envoyer(outil, dry_run=True)
    assert out["footer"] == "platform" and _LIEN not in out["html"]
    assert _NOTRE_MENTION in out["html"]


# ── le texte servi ───────────────────────────────────────────────────────────

def test_les_descriptions_servies_disent_le_geste(outil):
    from oto_mcp.capabilities.registry import CAPABILITIES
    assert "unsubscribe_url" in outil.description and "footer" in outil.description
    par_cle = {c.key: c for c in CAPABILITIES}
    for cle in ("org.settings.console", "org.email_settings.set"):
        assert "unsubscribe_url" in par_cle[cle].description
        assert "clear_footer" in par_cle[cle].description
