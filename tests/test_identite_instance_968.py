"""L'identité de l'instance se DÉCLARE, sinon l'instance ne démarre pas (#968).

Décision du 28/09/2026 : refus partout. Deux choses sont éprouvées ici.

1. **Le refus** — chaque variable de la classe IDENTITE de l'inventaire, retirée seule,
   fait échouer `identite_instance.verifier` en la nommant. Le banc part de
   l'INVENTAIRE, pas d'une liste recopiée : une variable ajoutée à la classe sans
   accesseur vérifié au démarrage rougit ici.
2. **L'instance déclarée** — montée avec les seules variables que l'inventaire exige,
   aux valeurs d'un tiers, elle n'émet aucune de nos adresses sur les surfaces qui en
   émettaient : lien d'invitation, contrats, pied d'email, pages publiques partagées,
   CORS, adresse du tableau de bord, contact des sondes sortantes.

⚠️ Ce second banc n'est PAS le montage réel d'une instance (ni base, ni Logto, ni
serveur servi) : il rend les surfaces une à une. Il prouve que ces surfaces lisent la
déclaration ; il ne prouve pas qu'aucune autre surface n'émet de lien chez nous.
"""
from __future__ import annotations

import json

import pytest

from oto_mcp import config, env_inventory as inv, identite_instance

# Une instance qui n'est pas nous : aucune de ces valeurs ne porte un de nos domaines.
_TIERCE = {
    "OTO_MCP_PUBLIC_URL": "https://mcp.tiers.test",
    "OTO_PROJECT_DOMAIN": "tiers.test",
    "OTO_APP_URL": "https://app.tiers.test",
    "OTO_DASHBOARD_URL": "https://app.tiers.test",
    "OTO_DASHBOARD_BASE_URL": "https://app.tiers.test",
    "OTO_INVITE_BASE_URL": "https://tiers.test",
    "OTO_MCP_CORS_ORIGINS": "https://app.tiers.test",
    "OTO_MAILER_URL": "https://mailer.tiers.test/api/send",
    "OTO_MAIL_FROM": "Tiers <bonjour@tiers.test>",
    "OTO_CONTACT_TO": "contact@tiers.test",
    "OTO_LEGAL_DOCS": json.dumps({
        slug: {"version": "1.0", "label": slug.upper(), "url": f"https://tiers.test/{slug}"}
        for slug in ("terms", "cgv", "dpa")}),
    "OTO_BRAND_NAME": "Tiers",
    "OTO_BRAND_SITE": "tiers.test",
}
_NOS_DOMAINES = ("oto.cx", "oto.ninja", "oto.zone", "otomata")
_IDENTITE = [v.nom for v in inv.NOMS_FIXES if v.classe is inv.Classe.IDENTITE]


@pytest.fixture
def tierce(monkeypatch):
    """L'environnement d'une instance tierce, déclarée depuis le seul inventaire."""
    for nom in _IDENTITE:
        monkeypatch.delenv(nom, raising=False)
    for nom, valeur in _TIERCE.items():
        monkeypatch.setenv(nom, valeur)
    return monkeypatch


def test_le_banc_declare_toute_la_classe_identite():
    """Si l'inventaire gagne une variable d'identité, ce banc doit la déclarer — sinon
    le refus ci-dessous passerait pour une raison qui n'est pas la bonne."""
    assert set(_IDENTITE) == set(_TIERCE)


def test_une_instance_entierement_declaree_demarre(tierce):
    identite_instance.verifier()


@pytest.mark.parametrize("nom", _IDENTITE)
def test_chaque_variable_d_identite_manquante_refuse_le_demarrage(tierce, nom):
    # La cascade du tableau de bord : une seule des trois suffit, donc « manquante »
    # veut dire les trois absentes.
    groupe = config.DASHBOARD_VARS if nom in config.DASHBOARD_VARS else (nom,)
    for n in groupe:
        tierce.delenv(n)
    with pytest.raises(identite_instance.IdentiteNonDeclaree) as refus:
        identite_instance.verifier()
    assert nom in str(refus.value) or nom in config.DASHBOARD_VARS, str(refus.value)


def test_le_refus_nomme_tout_ce_qui_manque_d_un_coup(monkeypatch):
    for nom in _IDENTITE:
        monkeypatch.delenv(nom, raising=False)
    with pytest.raises(identite_instance.IdentiteNonDeclaree) as refus:
        identite_instance.verifier()
    message = str(refus.value)
    for nom in _IDENTITE:
        if nom not in config.DASHBOARD_VARS:
            assert nom in message, nom


@pytest.mark.parametrize("valeur, attendu", [
    ("{", "JSON"),
    ('{"terms": {"version": "1", "label": "CGU", "url": "https://t.test"}}', "attendus"),
    (json.dumps({s: {"version": "1", "label": "L", "url": ""} for s in ("terms", "cgv", "dpa")}),
     "url"),
])
def test_des_documents_legaux_mal_declares_refusent(tierce, valeur, attendu):
    from oto_mcp import legal_docs
    tierce.setenv("OTO_LEGAL_DOCS", valeur)
    with pytest.raises(legal_docs.DocumentsLegauxMalDeclares, match=attendu):
        legal_docs.current_docs()


@pytest.mark.parametrize("site", ["https://tiers.test", "tiers.test/", "tiers.test:443"])
def test_le_site_de_marque_est_un_hote_nu(tierce, site):
    from oto_mcp import email_brand
    tierce.setenv("OTO_BRAND_SITE", site)
    with pytest.raises(RuntimeError, match="OTO_BRAND_SITE"):
        email_brand.marque_instance()


def test_une_origine_cors_vide_refuse(tierce):
    tierce.setenv("OTO_MCP_CORS_ORIGINS", " , ")
    with pytest.raises(RuntimeError, match="aucune origine"):
        config.cors_origins()


# ── l'instance déclarée n'émet rien chez nous ─────────────────────────────────

def _surfaces(monkeypatch) -> dict[str, str]:
    """Ce que l'instance émet sous son nom, rendu depuis sa seule déclaration."""
    from oto_mcp import email, legal_docs, public_doc_page, share_ui, tenancy
    from oto_mcp.api import base
    from oto_mcp.capabilities.orgs import invites
    from oto_mcp.tools import infosec

    envoye: dict = {}
    monkeypatch.setattr(email, "_send",
                        lambda to, subject, html, **k: envoye.update(html=html, **k) or True)
    email.send_composed_email("dest@exemple.test", "sujet", "bonjour")
    return {
        "lien d'invitation": invites._nominal_url("jeton"),
        "contrats": json.dumps(legal_docs.docs_for(tenancy.PRIMARY_SLUG)),
        "email composé": envoye["html"] + str(envoye.get("reply_to")),
        "page de projet partagé": share_ui.render_not_found(),
        "page de document partagé": public_doc_page.render_missing(),
        "origines CORS": ",".join(base._allowed_origins()),
        "tableau de bord": config.dashboard_url(),
        "contact des sondes": infosec._ua(),
    }


def test_l_instance_declaree_n_emet_aucune_de_nos_adresses(tierce):
    for surface, rendu in _surfaces(tierce).items():
        trouves = [d for d in _NOS_DOMAINES if d in rendu.lower()]
        assert not trouves, f"{surface} émet {trouves} : {rendu[:300]}"
        assert "tiers" in rendu.lower(), f"{surface} ne porte pas la déclaration : {rendu[:300]}"


def test_le_banc_mord_sur_notre_propre_declaration(monkeypatch):
    """Preuve, pas affirmation : avec NOTRE déclaration (celle que gréé `conftest`),
    chaque surface porte nos adresses — le banc ci-dessus sait donc les voir."""
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.oto.cx")  # conftest gréé un décoy
    for surface, rendu in _surfaces(monkeypatch).items():
        assert any(d in rendu.lower() for d in _NOS_DOMAINES), surface
