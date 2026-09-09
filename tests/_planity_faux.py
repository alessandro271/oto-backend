"""Le harnais des tests du connecteur `planity` — le cœur moqué à SA frontière.

⚠️ **Le cœur est moqué**, et pas par confort : le venv de ce dépôt porte une COPIE
FIGÉE d'oto-core au tag épinglé, qui peut ne pas encore contenir le paquet. Un test
qui importerait le vrai cœur mesurerait l'âge du venv, pas le code de ce dépôt. Ce
que les tests vérifient est donc exactement le périmètre du backend : la
déclaration au registre, la résolution du credential, la vie de la session, et le
passage des arguments et des conversions à la frontière des outils.

⚠️ **Aucun test ne parle à Planity.** Rien ici n'ouvre de connexion, et aucun
identifiant réel n'existe dans ce dépôt.

Ce module est un HELPER (préfixe `_`), pas un fichier de tests : il vit ici parce
que trois fichiers de tests s'en servent, et qu'un `conftest.py` l'imposerait à
tout le dépôt pour trois consommateurs.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

#: Le fuseau du salon, comme dans le cœur. La doublure de `ms_to_iso` rend un ISO
#: VRAI et pas un `f"iso:{ms}"` décoratif : `periode()` en relit la date, et une
#: doublure qui n'est pas une date fait échouer le test sur la doublure.
_TZ = ZoneInfo("Europe/Paris")


def _iso(ms):
    """La doublure de `ms_to_iso` : même contrat que le cœur."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=_TZ).isoformat(timespec="seconds")


#: Le venv de ce dépôt peut porter un oto-core ANTÉRIEUR aux sous-modules
#: `services`/`stock` (le pin du pyproject les exige, `pip` ne réinstalle pas une
#: dépendance VCS déjà présente). Les tests qui en dépendent sont alors SAUTÉS en
#: le disant, jamais verts sur une doublure qui approuverait n'importe quoi.


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
    mod.ms_to_iso = _iso
    # Les sous-modules du cœur que les outils appellent pour APLATIR (prix d'une
    # prestation, lots d'un produit). Ce sont des fonctions pures : les moquer
    # mesurerait la doublure, pas le tri. On pose donc les vraies, quand le venv
    # les a — et on saute proprement quand il porte encore un oto-core sans elles.
    mod.services = _sous_module("services")
    mod.stock = _sous_module("stock")
    return mod


def _sous_module(nom):
    """Le vrai sous-module du cœur, ou une doublure qui DIT ce qui manque.

    Le venv de ce dépôt porte oto-core AU TAG ÉPINGLÉ. Tant que le tag ne contient
    pas ces modules, un test qui en dépend n'a rien à mesurer : il est SAUTÉ en le
    disant, plutôt que vert sur une doublure qui approuverait n'importe quoi."""
    import importlib

    try:
        return importlib.import_module(f"oto.tools.planity.{nom}")
    except ImportError:
        return None


def _client_moque():
    """Un `PlanityClient` dont chaque méthode est attendue (le cœur est async)."""
    c = MagicMock()
    for nom in ("list_salons", "get_salon", "list_services", "list_products",
                "search_customers", "get_customer", "get_customer_stats",
                "get_customer_receipts", "get_appointment",
                "get_key_indicators", "get_revenues", "get_best_customers",
                "get_new_customers", "get_overall_frequencies",
                "get_revenue_breakdown", "get_calendar_stats",
                "get_occupancy_rate", "get_reviews_stats",
                "get_revenue_by_payment_method", "get_revenue_by_vat",
                "get_service_stats", "get_pos_period", "get_receipt",
                "list_payment_methods", "list_product_orders", "close"):
        setattr(c, nom, AsyncMock(return_value={}))
    for nom in ("list_appointments", "list_recurring_appointments",
                "list_pos_periods", "list_stock_movements",
                "list_mass_stock_removals", "list_suppliers"):
        setattr(c, nom, AsyncMock(return_value=[]))
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


def _employe(eid="emp-1", name="Alex", deleted_at=None, type_=None, title=None):
    """⚠️ Les champs sont posés EXPLICITEMENT, `deleted` compris.

    Un `MagicMock` rend un objet vrai pour tout attribut qu'on ne pose pas : un
    `e.deleted` non posé est truthy, donc tout enfant d'agenda passerait pour
    supprimé et la liste sortirait vide — un test vert sur un filtre inversé."""
    e = MagicMock()
    e.id, e.name, e.color, e.calendar_id = eid, name, "#111", "cal-1"
    e.deleted_at, e.type, e.title = deleted_at, type_, title
    e.deleted = deleted_at is not None
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
    from oto_mcp.tools import planity, planity_pos, planity_stats, planity_stock

    m = FastMCP("t")
    for module in (planity, planity_stats, planity_pos, planity_stock):
        module.register(m)
    return m


def _outil(coeur, nom):
    return asyncio.run(_serveur(coeur).get_tool(nom)).fn


exige_les_sous_modules = pytest.mark.skipif(
    _sous_module("services") is None or _sous_module("stock") is None,
    reason="oto-core de ce venv sans oto.tools.planity.services/stock — la CI, "
           "qui installe AU tag épinglé, les a (cf. tests/_oto_core_pin.py)")
