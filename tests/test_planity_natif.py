"""Le connecteur `planity`, natif — ce qui change et ce qui ne doit PAS changer.

Jusqu'au 2026-09-09 les vingt outils `planity_*` étaient servis par un MCP distant
que nous opérions (`kind="mount"`) ; le backend lui rejouait le `basic_auth` du
coffre par requête. Ils sont désormais montés en propre (`kind="tools"`), sur le
cœur d'oto-core. **Le credential ne bouge pas d'un iota, les noms d'outils non
plus** : ce sont les deux choses qu'un agent déjà écrit et une fiche déjà lue
tiennent pour acquises, et les deux qu'un changement de plomberie casserait sans
que rien ne le dise.

⚠️ **Le cœur est MOQUÉ ici, à sa frontière** (`oto.tools.planity` posé dans
`sys.modules`), et pas par confort : le venv de ce dépôt porte une COPIE FIGÉE
d'oto-core au tag épinglé, qui ne contient pas encore ce paquet. Un test qui
importerait le vrai cœur mesurerait l'âge du venv, pas le code de ce dépôt. Ce
qu'on vérifie ici est donc exactement le périmètre du backend : la déclaration au
registre, la résolution du credential, la vie de la session, et le passage des
arguments et des conversions à la frontière des outils.

⚠️ **Aucun appel réel à Planity n'a jamais été joué** : aucun identifiant Planity
n'existe au coffre à l'écriture de ce fichier. Rien ici ne le prétend.
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from oto_mcp import providers


# ── Le cœur, moqué à sa frontière ───────────────────────────────────────────

#: Coordonnées MANIFESTEMENT fictives. Elles appartiennent à Planity, sont
#: publiques par conception (tout navigateur qui ouvre `pro.planity.com` les
#: reçoit), et se
#: posent par l'OPÉRATEUR de l'instance, en base (`connector_settings`, scope
#: plateforme) : rien de tout ça ne vit dans le dépôt.
_COORDONNEES = {
    "firebase_api_key": "cle-firebase-fictive",
    "firebase_app_id": "app-id-fictif",
    "rest_api": "https://api.exemple.invalid",
}


def _lignes(poses: dict) -> list[dict]:
    """La forme que rend `db.connector_settings.list_connector_settings`."""
    return [{"scope_type": "platform", "scope_id": "platform",
             "connector": "planity", "key": k, "value": v}
            for k, v in poses.items()]


def _poser(monkeypatch, poses: dict) -> None:
    from oto_mcp.db import connector_settings as store
    monkeypatch.setattr(store, "list_connector_settings",
                        lambda key=None, conn=None: _lignes(poses))


def _faux_coeur(client=None):
    """Un module `oto.tools.planity` réduit à ce que le backend en importe."""
    mod = types.ModuleType("oto.tools.planity")
    mod.PlanityClient = MagicMock(return_value=client or MagicMock())
    mod.PlanityEndpoints = MagicMock(name="PlanityEndpoints")
    mod.resolve_range = MagicMock(return_value=(1_000, 2_000))
    mod.ms_to_iso = lambda ms: None if not ms else f"iso:{int(ms)}"
    return mod


def _client_moque():
    """Un `PlanityClient` dont chaque méthode est attendue (le cœur est async)."""
    c = MagicMock()
    for nom in ("list_salons", "get_salon", "list_services", "list_products",
                "search_customers", "get_customer", "get_customer_stats",
                "get_customer_receipts", "list_appointments", "get_appointment",
                "get_key_indicators", "get_revenues", "get_best_customers",
                "get_new_customers", "get_overall_frequencies",
                "get_revenue_breakdown", "get_calendar_stats",
                "get_occupancy_rate", "get_reviews_stats", "close"):
        setattr(c, nom, AsyncMock(return_value={}))
    c.auth = MagicMock(get_tokens=AsyncMock(return_value=object()))
    return c


def _salon(**kw):
    s = MagicMock()
    s.id = kw.get("id", "biz-un")
    s.name = kw.get("name", "Salon Exemple")
    s.slug = "salon-exemple"
    s.phone = "0100000000"
    s.opening_hours = "10:00-19:00"
    s.db_shard = "fr-00"
    s.calendars = kw.get("calendars", ["cal-1"])
    s.employees = kw.get("employees", [])
    return s


def _employe(eid="emp-1", name="Alex"):
    e = MagicMock()
    e.id, e.name, e.color, e.calendar_id = eid, name, "#111", "cal-1"
    return e


@pytest.fixture
def coeur(monkeypatch):
    """Pose le faux cœur, un credential résolu, et une session vierge."""
    client = _client_moque()
    mod = _faux_coeur(client)
    monkeypatch.setitem(sys.modules, "oto.tools.planity", mod)
    _poser(monkeypatch, _COORDONNEES)
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential_fields",
        lambda provider, account=None: {"email": "demo@example.com",
                                        "password": "s3cret"})
    from oto_mcp.tools import planity_session
    planity_session._entrees.clear()
    yield types.SimpleNamespace(module=mod, client=client)
    planity_session._entrees.clear()


def _serveur(coeur):
    from fastmcp import FastMCP
    from oto_mcp.tools import planity, planity_stats

    m = FastMCP("t")
    planity.register(m)
    planity_stats.register(m)
    return m


def _outil(coeur, nom):
    return asyncio.run(_serveur(coeur).get_tool(nom)).fn


# ── La déclaration au registre ──────────────────────────────────────────────

def test_planity_est_un_connecteur_natif_et_plus_un_mcp_federe():
    c = providers.REGISTRY["planity"]
    assert c.kind == "tools"
    # `family` est DÉRIVÉE du kind : elle disait « federated », elle dit « api ».
    assert c.family == "api"


def test_le_credential_de_planity_ne_change_pas():
    """Ce qui est au coffre a été posé sous ce format et doit continuer de se
    relire : le passage en natif est une affaire de plomberie, pas de credential."""
    c = providers.REGISTRY["planity"]
    assert c.auth_modes == frozenset({"byo_user"})
    assert c.secret_kind == "basic_auth"
    assert [f.name for f in c.secret_fields] == ["email", "password"]
    assert c.auth["method"] == "secret"


def test_la_fiche_ne_renvoie_plus_vers_un_service_a_nous():
    """L'adresse du serveur que nous opérions n'a plus rien à faire dans la fiche :
    elle enverrait sur un service que le connecteur n'appelle plus."""
    c = providers.REGISTRY["planity"]
    texte = " ".join([c.help, c.href or "", c.description])
    assert "planity-mcp" not in texte and "oto.zone" not in texte
    assert "passerelle" not in texte.lower()
    assert c.publisher_name == "Otomata"


def test_les_deux_modules_declares_sont_ceux_qui_montent_les_outils():
    assert providers.REGISTRY["planity"].modules == ("planity", "planity_stats")


# ── La surface servie ───────────────────────────────────────────────────────

_ATTENDUS = sorted([
    "planity_list_salons", "planity_get_salon_info", "planity_list_employees",
    "planity_list_services", "planity_list_products",
    "planity_search_customers", "planity_get_customer",
    "planity_get_customer_stats", "planity_get_customer_receipts",
    "planity_list_appointments", "planity_get_appointment",
    "planity_get_revenue_summary", "planity_get_daily_revenue",
    "planity_get_best_customers", "planity_get_new_customers",
    "planity_get_customer_frequencies", "planity_get_revenue_breakdown",
    "planity_get_seller_stats", "planity_get_occupancy_rate",
    "planity_get_reviews_stats",
])


def test_les_vingt_outils_gardent_exactement_leurs_noms(coeur):
    """La liste EN DUR, pas dérivée du module : c'est le contrat qu'un agent déjà
    écrit tient pour acquis. Un renommage, un oubli ou un ajout se voit ici."""
    servis = sorted(t.name for t in asyncio.run(_serveur(coeur).list_tools()))
    assert servis == _ATTENDUS
    assert len(servis) == 20


def test_tous_les_outils_sont_dans_le_namespace_du_connecteur(coeur):
    """Le gate d'activation lit le PREMIER token du nom : un outil qui sortirait du
    préfixe échapperait à l'activation du connecteur, en silence."""
    ns = providers.REGISTRY["planity"].namespaces[0]
    for t in asyncio.run(_serveur(coeur).list_tools()):
        assert t.name.split("_")[0] == ns


# ── L'instance non configurée : présent, et refusant en le disant ───────────

def test_les_outils_sont_montes_meme_sans_coordonnees(monkeypatch):
    """⚠️ Le connecteur ne DISPARAÎT pas quand l'instance n'est pas configurée.

    Un connecteur absent du catalogue ne se remarque pas et ne s'explique pas :
    l'utilisatrice ne voit rien, ne peut rien demander, et personne ne sait
    pourquoi. Présent et refusant en nommant la cause, elle lit le motif et
    l'exploitant sait quoi poser. C'est l'inverse du réflexe « ne monte pas ce qui
    ne marche pas », et c'est délibéré."""
    mod = _faux_coeur(_client_moque())
    monkeypatch.setitem(sys.modules, "oto.tools.planity", mod)
    _poser(monkeypatch, {})

    servis = sorted(t.name for t in asyncio.run(_serveur(None).list_tools()))
    assert servis == _ATTENDUS, "les 20 outils restent montés, configurés ou non"


def test_sans_coordonnees_l_appel_refuse_en_nommant_les_trois_variables(monkeypatch):
    """Et le refus ne parle PAS du credential : sans les coordonnées, l'appel
    partirait sur un 400 de Firebase, que la chaîne d'erreur traduirait — à raison
    dans son contexte, à tort ici — en « email ou mot de passe refusé ».
    L'utilisatrice reposerait alors un credential parfaitement bon, en boucle."""
    from oto_mcp.mcp_errors import McpError

    mod = _faux_coeur(_client_moque())
    monkeypatch.setitem(sys.modules, "oto.tools.planity", mod)
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: {"email": "demo@example.com",
                                                        "password": "s3cret"})
    from oto_mcp.tools import planity_session
    planity_session._entrees.clear()
    _poser(monkeypatch, {})

    outil = asyncio.run(_serveur(None).get_tool("planity_list_salons")).fn
    with pytest.raises(McpError) as e:
        asyncio.run(outil())
    msg = str(e.value)
    for nom in _COORDONNEES:
        assert nom in msg, f"{nom} doit être nommée dans le refus"
    assert "mot de passe" not in msg and "credential" in msg
    assert "oto_admin_connector_setting" in msg, (
        "un diagnostic qui ne dit pas le GESTE renvoie chercher — c'est ainsi "
        "qu'on relance six fois une configuration valide")


def test_une_seule_cle_manquante_est_nommee_seule(monkeypatch):
    """Nommer les trois quand une seule manque envoie tout revérifier.

    Une clé POSÉE mais vide compte comme absente : une ligne en base qu'on croit
    configurée et que personne ne peut lire est le pire des deux états."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import planity_session

    mod = _faux_coeur(_client_moque())
    monkeypatch.setitem(sys.modules, "oto.tools.planity", mod)
    _poser(monkeypatch, dict(_COORDONNEES, rest_api="   "))
    planity_session._entrees.clear()

    with pytest.raises(McpError) as e:
        planity_session.endpoints()
    msg = str(e.value)
    assert "rest_api" in msg
    assert "firebase_api_key" not in msg


def test_sans_l_extra_du_coeur_l_appel_refuse_en_nommant_l_extra(monkeypatch):
    """Même règle pour l'autre moitié de la configuration : le connecteur reste
    monté, et le refus nomme l'extra d'oto-core au lieu de rendre « No module named
    'httpx' » — vrai, et parfaitement inutile à qui le lit."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import planity_session

    monkeypatch.delitem(sys.modules, "oto.tools.planity", raising=False)
    _poser(monkeypatch, _COORDONNEES)
    planity_session._entrees.clear()

    servis = sorted(t.name for t in asyncio.run(_serveur(None).list_tools()))
    assert servis == _ATTENDUS, "les 20 outils restent montés sans le cœur"

    outil = asyncio.run(_serveur(None).get_tool("planity_list_salons")).fn
    with pytest.raises(McpError) as e:
        asyncio.run(outil())
    assert "oto-core[planity]" in str(e.value)


def test_le_demarrage_dit_ce_qui_manque(monkeypatch, caplog):
    """Sans cette ligne au boot, un exploitant qui a oublié une variable ne
    l'apprendrait qu'au premier appel d'une utilisatrice."""
    from oto_mcp.tools import planity_session

    _poser(monkeypatch, {})
    with caplog.at_level("WARNING"):
        planity_session.avertir_au_demarrage()
    for nom in _COORDONNEES:
        assert nom in caplog.text
    assert "oto_admin_connector_setting" in caplog.text


# ── La session par credential ───────────────────────────────────────────────

def test_le_client_est_construit_avec_le_credential_du_coffre(coeur):
    _outil(coeur, "planity_list_salons")
    asyncio.run(_outil(coeur, "planity_list_salons")())
    appel = coeur.module.PlanityClient.call_args
    assert appel.args[:2] == ("demo@example.com", "s3cret")
    assert appel.args[2] is coeur.module.PlanityEndpoints.return_value, (
        "le client doit recevoir les coordonnées de l'instance — sans elles, "
        "oto-core lève à la construction")


def test_la_session_valide_le_credential_a_l_ouverture(coeur):
    """L'auth est jouée à l'ouverture, pas au premier appel métier : un mot de passe
    faux doit dire « connexion refusée », pas « salon inaccessible » plus tard."""
    asyncio.run(_outil(coeur, "planity_list_salons")())
    coeur.client.auth.get_tokens.assert_awaited()


def test_deux_appels_du_meme_credential_partagent_la_meme_session(coeur):
    """Une session Planity coûte trois allers-retours d'auth puis un WebSocket : la
    rouvrir à chaque appel rendrait le connecteur inutilisable."""
    outil = _outil(coeur, "planity_list_salons")

    async def _deux():
        await outil()
        await outil()

    asyncio.run(_deux())
    assert coeur.module.PlanityClient.call_count == 1


def test_un_autre_credential_ouvre_une_autre_session(coeur, monkeypatch):
    outil = _outil(coeur, "planity_list_salons")
    asyncio.run(outil())

    comptes = iter([{"email": "a@example.com", "password": "p1"},
                    {"email": "b@example.com", "password": "p2"}])
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: next(comptes))

    async def _deux():
        await outil()
        await outil()

    asyncio.run(_deux())
    assert coeur.module.PlanityClient.call_count == 3


def test_une_ouverture_en_echec_n_est_pas_mise_en_cache(coeur):
    """Sinon un mot de passe corrigé continuerait d'échouer jusqu'à l'expiration."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import planity_session

    coeur.client.auth.get_tokens.side_effect = RuntimeError("réseau")
    outil = _outil(coeur, "planity_list_salons")
    with pytest.raises(McpError):
        asyncio.run(outil())
    assert planity_session._entrees == {}

    coeur.client.auth.get_tokens.side_effect = None
    asyncio.run(outil())
    assert coeur.module.PlanityClient.call_count == 2


def test_un_credential_incomplet_est_refuse_en_le_disant(coeur, monkeypatch):
    from oto_mcp.mcp_errors import McpError

    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: {"email": "demo@example.com",
                                                        "password": ""})
    with pytest.raises(McpError) as e:
        asyncio.run(_outil(coeur, "planity_list_salons")())
    assert "mot de passe" in str(e.value)


def test_un_refus_d_auth_de_planity_dit_de_reposer_le_credential(coeur):
    """Un 400 de Firebase veut dire « cet email ou ce mot de passe ne va pas ».
    Rendu brut, il se lit comme une panne de service — donc « réessaie », alors que
    réessayer ne peut pas aboutir."""
    from oto_mcp.mcp_errors import McpError

    refus = RuntimeError("400")
    refus.response = types.SimpleNamespace(status_code=400)
    coeur.client.auth.get_tokens.side_effect = refus

    with pytest.raises(McpError) as e:
        asyncio.run(_outil(coeur, "planity_list_salons")())
    msg = str(e.value)
    assert "mot de passe" in msg and "credential" in msg


def test_aucun_refus_ne_recrache_le_credential(coeur):
    """LE credential de ce connecteur est un MOT DE PASSE, et il est passé en
    argument à trois fonctions : n'importe quelle exception construite avec ces
    arguments se retrouverait mot pour mot dans la réponse rendue à l'agent — donc
    dans un transcript, dans le journal d'appels, chez l'utilisateur suivant.

    On exerce le PIRE cas, fabriqué exprès : une exception dont le texte porte
    l'email et le mot de passe. Aucun amont ne fait ça aujourd'hui (httpx met
    l'URL dans ses messages, jamais le corps) — c'est précisément pourquoi la
    porte se ferme maintenant, pendant qu'il n'y a pas d'incident à raconter. Un
    test qui n'exercerait que les exceptions réelles ne garderait rien."""
    from oto_mcp.mcp_errors import McpError

    coeur.client.auth.get_tokens.side_effect = RuntimeError(
        "refus pour demo@example.com / s3cret")
    with pytest.raises(McpError) as e:
        asyncio.run(_outil(coeur, "planity_list_salons")())
    msg = str(e.value)
    assert "s3cret" not in msg and "demo@example.com" not in msg
    assert "RuntimeError" in msg, "le type reste dit — sinon le refus n'aide plus"


def test_le_journal_ne_porte_pas_le_credential(coeur, caplog):
    """Même règle sur l'autre sortie : ce qui n'a pas le droit d'aller à l'agent
    n'a pas plus le droit d'aller au journal, qui vit plus longtemps."""
    coeur.client.auth.get_tokens.side_effect = RuntimeError(
        "refus pour demo@example.com / s3cret")
    with caplog.at_level("DEBUG"):
        with pytest.raises(Exception):
            asyncio.run(_outil(coeur, "planity_list_salons")())
    assert "s3cret" not in caplog.text and "demo@example.com" not in caplog.text


# ── Une famille d'outils à la fois, cœur moqué ──────────────────────────────

def test_referentiel_un_salon_est_rendu_avec_ses_compteurs(coeur):
    coeur.client.list_salons.return_value = [
        _salon(employees=[_employe(), _employe("emp-2", "Camille")])]
    out = asyncio.run(_outil(coeur, "planity_list_salons")())
    assert out == [{"id": "biz-un", "name": "Salon Exemple", "slug": "salon-exemple",
                    "phone": "0100000000", "opening_hours": "10:00-19:00",
                    "employee_count": 2, "calendar_count": 1}]


def test_referentiel_les_prestations_sont_aplaties_et_en_euros(coeur):
    """Planity range les prestations sous des catégories et compte en CENTIMES.
    Rendre 4500 au lieu de 45 € ne lève rien : ça se lit comme un tarif."""
    coeur.client.list_services.return_value = {
        "cat-1": {"children": {"svc-1": {"name": " Coupe ", "price": 4500,
                                         "duration": 30}}}}
    out = asyncio.run(_outil(coeur, "planity_list_services")(salon_id="biz-un"))
    assert out == [{"id": "svc-1", "category_id": "cat-1", "name": "Coupe",
                    "price_eur": 45.0, "duration_minutes": 30, "bookable": True,
                    "description": ""}]


def test_clientes_les_horodatages_passent_par_la_conversion_du_coeur(coeur):
    coeur.client.search_customers.return_value = [
        {"objectID": "cli-1", "name": " Cliente Exemple ", "createdAt": 1_700_000}]
    out = asyncio.run(_outil(coeur, "planity_search_customers")(salon_id="biz-un"))
    assert out[0]["id"] == "cli-1" and out[0]["name"] == "Cliente Exemple"
    assert out[0]["created_at"] == "iso:1700000"


def test_clientes_un_ticket_totalise_ses_lignes_en_euros(coeur):
    coeur.client.get_customer_receipts.return_value = [
        {"receiptId": "r-1", "createdAt": 2_000,
         "lines": [{"price": 4500, "serviceId": "svc-1"},
                   {"price": 1200, "productId": "prd-1"}]}]
    out = asyncio.run(_outil(coeur, "planity_get_customer_receipts")(
        salon_id="biz-un", customer_id="cli-1"))
    assert out[0]["total_eur"] == 57.0
    assert [l["price_eur"] for l in out[0]["lines"]] == [45.0, 12.0]


def test_agenda_la_fenetre_de_dates_filtre_vraiment(coeur):
    """Les rendez-vous arrivent tous ensemble du Realtime Database : c'est ICI que
    la fenêtre s'applique. Sans ce filtre, `preset="today"` rendrait l'année."""
    coeur.client.list_appointments.return_value = {
        "v-dedans": {"start": 1_500, "end": 1_800, "sellerId": "emp-1"},
        "v-avant": {"start": 10, "end": 20},
        "v-apres": {"start": 9_000, "end": 9_100},
    }
    out = asyncio.run(_outil(coeur, "planity_list_appointments")(
        salon_id="biz-un", preset="today"))
    assert out["count"] == 1 and out["vevents"][0]["id"] == "v-dedans"
    assert (out["from"], out["to"]) == ("iso:1000", "iso:2000")
    coeur.module.resolve_range.assert_called_with(None, None, "today")


def test_agenda_le_filtre_par_collaboratrice_lit_les_trois_noms_de_champ(coeur):
    """Planity nomme le vendeur `seller_id`, `sellerId` ou `child` selon l'âge de
    l'enregistrement — n'en lire qu'un rend un agenda vide, pas une erreur."""
    coeur.client.list_appointments.return_value = {
        "a": {"start": 1_500, "child": "emp-1"},
        "b": {"start": 1_500, "sellerId": "emp-2"},
    }
    out = asyncio.run(_outil(coeur, "planity_list_appointments")(
        salon_id="biz-un", employee_id="emp-1"))
    assert [v["id"] for v in out["vevents"]] == ["a"]


def test_chiffres_le_ca_est_rendu_en_euros_avec_ses_bornes(coeur):
    coeur.client.get_key_indicators.return_value = {
        "revenueWithVAT": 123_456, "revenueWithoutVAT": 102_880,
        "amountOfReceipts": 12, "VATValue": 20_576, "averageBasket": 10_288}
    out = asyncio.run(_outil(coeur, "planity_get_revenue_summary")(
        salon_id="biz-un", preset="last_month"))
    assert out["revenue_ttc_eur"] == 1234.56 and out["revenue_ht_eur"] == 1028.8
    assert out["ticket_count"] == 12
    assert (out["from"], out["to"]) == ("iso:1000", "iso:2000")


def test_chiffres_les_collaboratrices_sont_nommees_et_classees(coeur):
    """Les statistiques arrivent en TUPLES anonymes : le nom vient du référentiel,
    et une ligne trop courte est ignorée plutôt que lue de travers."""
    coeur.client.get_salon.return_value = _salon(
        employees=[_employe("emp-1", "Alex"), _employe("emp-2", "Camille")])
    coeur.client.get_calendar_stats.return_value = {
        "data": [["emp-1", 3, 4, None, 30_000, 100.0],
                 ["emp-2", 9, 12, None, 90_000, 75.0],
                 ["emp-tronque", 1]],
        "bySeller": {"data": {"emp-2": {"onlineAppointments": 4}}},
    }
    out = asyncio.run(_outil(coeur, "planity_get_seller_stats")(salon_id="biz-un"))
    assert [(s["seller_id"], s["name"], s["revenue_eur"]) for s in out] == [
        ("emp-2", "Camille", 900.0), ("emp-1", "Alex", 300.0)]
    assert out[0]["appointments"] == {"onlineAppointments": 4}


# ── La sonde de connexion ───────────────────────────────────────────────────

def test_le_connecteur_est_verifiable(coeur):
    from oto_mcp.connectors import verify as connector_verify

    _serveur(coeur)                       # `register()` pose la sonde
    assert connector_verify.supports("planity")
    assert connector_verify.couverture("planity") == connector_verify.AUTH


def test_la_sonde_ouvre_la_session_puis_liste_les_salons(coeur):
    from oto_mcp.connectors import verify as connector_verify
    from oto_mcp.tools import planity

    _serveur(coeur)
    coeur.client.list_salons.return_value = [_salon()]
    asyncio.run(connector_verify.run(
        "planity", {"email": "demo@example.com", "password": "s3cret"}))
    coeur.client.auth.get_tokens.assert_awaited()
    coeur.client.list_salons.assert_awaited()
    coeur.client.close.assert_awaited()


def test_la_sonde_refuse_un_compte_qui_n_ouvre_aucun_salon(coeur):
    """Authentifié ≠ utilisable : un compte dont le jeton ne porte aucun salon
    s'authentifie parfaitement et ne peut rien lire. Le rendre « connecté » serait
    le vert creux que la sonde existe pour empêcher."""
    from oto_mcp.connectors import verify as connector_verify

    _serveur(coeur)
    coeur.client.list_salons.return_value = []
    with pytest.raises(connector_verify.NonAutorise):
        asyncio.run(connector_verify.run(
            "planity", {"email": "demo@example.com", "password": "s3cret"}))
    assert connector_verify.classer(
        connector_verify.NonAutorise("x")) == connector_verify.UNAUTHORIZED


def test_la_sonde_classe_un_refus_d_identifiants_et_ne_le_confond_pas_avec_une_panne(coeur):
    from oto_mcp.connectors import verify as connector_verify

    _serveur(coeur)
    refus = RuntimeError("400")
    refus.response = types.SimpleNamespace(status_code=400)
    coeur.client.auth.get_tokens.side_effect = refus
    with pytest.raises(connector_verify.NonAutorise):
        asyncio.run(connector_verify.run(
            "planity", {"email": "demo@example.com", "password": "faux"}))
    coeur.client.close.assert_awaited()
