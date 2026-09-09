"""Ce que les outils Planity rendent — et surtout ce qu'ils ne rendent PAS.

Trois familles de règles vivent ici, et aucune ne se voit à la relecture :

- **la liste blanche.** Un rendez-vous et un ticket portent, chez Planity, le nom,
  le téléphone, l'email et l'adresse de la cliente. Rien de tout ça ne sort. Ce
  n'est pas une projection à « compléter » un jour au nom de « expose le brut » :
  c'est le parti pris, borné là où il coûterait à quelqu'un qui n'est pas dans la
  conversation. Le test balaie la réponse EN PROFONDEUR, parce qu'un champ interdit
  réapparaît toujours par un sous-objet qu'on avait oublié de regarder ;
- **supprimé n'est pas absent.** Planity garde ce qu'on retire ; le compter comme
  vivant fait annoncer sept collaboratrices à un salon qui en a trois ;
- **la fenêtre se dit.** Un chiffre d'affaires sans ses bornes invite à projeter
  dessus, et la plupart des fenêtres s'arrêtent à MAINTENANT — dernier jour
  partiel, rythme sous-estimé, projection fausse sans rien qui le signale.

Le harnais est dans `_planity_faux.py` ; aucun test ne parle à Planity.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from _planity_faux import (_employe, _iso, _outil, _salon, coeur,
                           exige_les_sous_modules)

from oto_mcp.mcp_errors import McpError

__all__ = ["coeur"]        # la fixture, importée pour être posée


#: Ce qui ne doit JAMAIS traverser la frontière d'un outil, à aucune profondeur.
#: `raw` en fait partie : c'est par lui que tout le reste revient d'un coup.
_INTERDITS = ("name", "phone", "email", "address", "postalCode", "city",
              "gender", "birth", "raw", "customer")

#: Un rendez-vous tel que le cœur le rend — la cliente EN CLAIR, comme Planity la
#: stocke. C'est exactement ce qui ne doit pas ressortir.
_RDV_COEUR = {
    "id": "vev-1", "child_id": "emp-1", "date": "2026-09-08",
    "start": "2026-09-08T14:30", "end": "2026-09-08T15:15",
    "duration_minutes": 45, "start_minutes": 870,
    "customer_id": "cli-1",
    "customer": {"id": "cli-1", "name": "Prénom Nom",
                 "phone": "0600000000", "email": "elle@exemple.invalid"},
    "service_id": "svc-1", "sequence": "seq-1", "price_cents": 4500,
    "comment": "commentaire qui cite un prénom",
    "title": "titre libre",
    "booked_via": "website", "cancelled": False, "cancelled_at": None,
    "created_at": 1_757_000_000_000, "updated_at": 1_757_000_100_000,
    "receipt": {"id": "tick-1", "period_id": "per-1"},
    "raw": {"cu": {"name": "Prénom Nom"}},
}

_TICKET_COEUR = {
    "id": "tick-1", "number": 12, "created_at": 1_757_000_000_000,
    "operation_type": "SALE", "lines_count": 1,
    "seller_id": "pro-1", "seller_name": "Alex",
    "customer_id": "cli-1",
    "customer": {"id": "cli-1", "name": "Prénom Nom", "phone": "0600000000",
                 "address": "1 rue Exemple", "city": "Ville",
                 "email": "elle@exemple.invalid", "comment": "note sur elle"},
    "lines": [{"title": "Coupe", "price_cents": 4500, "unit_price_cents": 4500,
               "quantity": 1, "service_id": "svc-1", "product_id": None,
               "seller_id": "pro-1", "vat_rate": 20, "vat_code": "A",
               "duration_minutes": 45, "from_appointment": True}],
    "payments": [{"method": "creditCard", "amount_cents": 4500, "tip_cents": 0}],
    "discount_total_cents": 0, "vat_included_total_cents": 4500,
    "vat_excluded_total_cents": 3750, "vat_total_cents": 750,
    "vat_rates": {"20": 750}, "cancelled": False, "cancelled_at": None,
    "cancelled_by": None, "appointment_ids": ["vev-1"],
    "raw": {"customer": {"name": "Prénom Nom"}},
}


def _cles(objet, chemin="") -> list[str]:
    """Tous les noms de clé d'une réponse, à toute profondeur."""
    trouve = []
    if isinstance(objet, dict):
        for k, v in objet.items():
            trouve.append(f"{chemin}.{k}")
            trouve += _cles(v, f"{chemin}.{k}")
    elif isinstance(objet, list):
        for x in objet:
            trouve += _cles(x, f"{chemin}[]")
    return trouve


def _valeurs(objet) -> list:
    """Toutes les valeurs scalaires d'une réponse, à toute profondeur."""
    if isinstance(objet, dict):
        return [x for v in objet.values() for x in _valeurs(v)]
    if isinstance(objet, list):
        return [x for v in objet for x in _valeurs(v)]
    return [objet]


def _aucun_champ_interdit(reponse, tolere=()):
    """Assertion partagée : aucune clé interdite, à aucune profondeur.

    Balayage PROFOND et par NOM de clé, pas sur l'objet de premier niveau : un
    champ interdit ne revient jamais là où on l'a retiré, il revient par un
    sous-objet qu'on n'a pas pensé à regarder."""
    fautifs = [c for c in _cles(reponse)
               if c.rsplit(".", 1)[-1] in _INTERDITS
               and c.rsplit(".", 1)[-1] not in tolere]
    assert not fautifs, f"champs de tiers rendus : {fautifs}"


# ── La liste blanche sur l'agenda ───────────────────────────────────────────

def test_un_rendez_vous_liste_ne_rend_ni_la_cliente_ni_le_commentaire(coeur):
    """Le commentaire d'un rendez-vous porte couramment des noms : il n'a pas à
    traverser une liste de cent pour répondre « qui vient jeudi »."""
    coeur.client.list_appointments.return_value = [_RDV_COEUR]
    out = asyncio.run(_outil(coeur, "planity_list_appointments")(salon_id="biz-un"))

    _aucun_champ_interdit(out)
    rdv = out["appointments"][0]
    assert rdv["customer_id"] == "cli-1"
    assert "comment" not in rdv and "title" not in rdv
    assert "Prénom Nom" not in _valeurs(out)


def test_un_rendez_vous_liste_garde_ce_qu_on_vient_y_chercher(coeur):
    """Une liste blanche trop serrée est un autre bug : ce qui reste doit suffire à
    lire une journée."""
    coeur.client.list_appointments.return_value = [_RDV_COEUR]
    rdv = asyncio.run(_outil(coeur, "planity_list_appointments")(
        salon_id="biz-un"))["appointments"][0]

    assert rdv["start"] == "2026-09-08T14:30" and rdv["end"] == "2026-09-08T15:15"
    assert rdv["duration_minutes"] == 45 and rdv["price_eur"] == 45.0
    assert rdv["service_id"] == "svc-1" and rdv["employee_id"] == "emp-1"
    assert rdv["booked_via"] == "website" and rdv["cancelled"] is False
    assert (rdv["receipt_id"], rdv["period_id"]) == ("tick-1", "per-1")


def test_un_rendez_vous_seul_rend_son_commentaire_mais_pas_la_cliente(coeur):
    """Appelé pour UN rendez-vous, l'outil rend la note qu'on est venu chercher.
    La cliente reste un identifiant : ce n'est pas elle qu'on a demandée."""
    coeur.client.get_appointment.return_value = _RDV_COEUR
    out = asyncio.run(_outil(coeur, "planity_get_appointment")(
        salon_id="biz-un", vevent_id="vev-1"))

    _aucun_champ_interdit(out)
    assert out["comment"] == "commentaire qui cite un prénom"
    assert out["title"] == "titre libre"
    assert out["customer_id"] == "cli-1"
    assert "Prénom Nom" not in _valeurs(out)


def test_un_rendez_vous_introuvable_le_dit_au_lieu_de_rendre_un_squelette(coeur):
    coeur.client.get_appointment.return_value = None
    out = asyncio.run(_outil(coeur, "planity_get_appointment")(
        salon_id="biz-un", vevent_id="inconnu"))
    assert out["error"] == "not_found"


def test_un_rendez_vous_annule_ressort_marque_et_non_filtre(coeur):
    """Planity n'a pas de statut, seulement une date de suppression. Les écarter
    d'office cacherait les annulations à qui les cherche."""
    coeur.client.list_appointments.return_value = [
        dict(_RDV_COEUR, cancelled=True, cancelled_at=1_757_000_200_000)]
    rdv = asyncio.run(_outil(coeur, "planity_list_appointments")(
        salon_id="biz-un"))["appointments"][0]
    assert rdv["cancelled"] is True
    assert rdv["cancelled_at"] == _iso(1_757_000_200_000)


def test_les_recurrents_ne_rendent_pas_la_cliente_non_plus(coeur):
    coeur.client.list_recurring_appointments.return_value = [{
        "id": "rec-1", "child_id": "emp-1", "rrule": "FREQ=WEEKLY",
        "duration_minutes": 30, "service_id": "svc-1", "sequence": None,
        "price_cents": 3000, "customer_id": "cli-1",
        "customer": {"id": "cli-1", "name": "Prénom Nom"},
        "created_at": 1, "updated_at": 2, "all_day": False,
        "title": "t", "comment": "c", "raw": {}}]
    out = asyncio.run(_outil(coeur, "planity_list_recurring_appointments")(
        salon_id="biz-un"))
    _aucun_champ_interdit(out)
    assert out[0]["rrule"] == "FREQ=WEEKLY" and out[0]["customer_id"] == "cli-1"


# ── La liste blanche sur la caisse ──────────────────────────────────────────

def test_un_ticket_ne_rend_de_la_cliente_que_son_identifiant(coeur):
    """Planity fige un instantané COMPLET de la cliente sur chaque ticket — nom,
    téléphone, email, adresse, commentaire. Rien n'en sort."""
    coeur.client.get_receipt.return_value = _TICKET_COEUR
    out = asyncio.run(_outil(coeur, "planity_get_receipt")(
        salon_id="biz-un", period_id="per-1", receipt_id="tick-1"))

    _aucun_champ_interdit(out)
    assert out["customer_id"] == "cli-1"
    assert "Prénom Nom" not in _valeurs(out)
    assert "1 rue Exemple" not in _valeurs(out)


def test_un_ticket_garde_ce_qui_fait_un_ticket(coeur):
    coeur.client.get_receipt.return_value = _TICKET_COEUR
    out = asyncio.run(_outil(coeur, "planity_get_receipt")(
        salon_id="biz-un", period_id="per-1", receipt_id="tick-1"))

    assert out["total_ttc_eur"] == 45.0 and out["total_ht_eur"] == 37.5
    assert out["vat_eur"] == 7.5 and out["number"] == 12
    assert out["seller_name"] == "Alex"
    assert out["lines"][0]["title"] == "Coupe"
    assert out["lines"][0]["price_eur"] == 45.0
    assert out["payments"][0] == {"method": "creditCard", "amount_eur": 45.0,
                                 "tip_eur": 0.0}
    assert out["appointment_ids"] == ["vev-1"]


def test_une_periode_de_caisse_projette_chacun_de_ses_tickets(coeur):
    """C'est le chemin par lequel un instantané de cliente reviendrait en nombre :
    dix tickets non projetés, dix fiches complètes."""
    coeur.client.get_pos_period.return_value = {
        "id": "per-1", "created_at": 1, "closed_at": 2, "open": False,
        "initial_amount_cents": 5000, "final_amount_cents": 9500,
        "receipts_count": 1, "receipts": [_TICKET_COEUR]}
    out = asyncio.run(_outil(coeur, "planity_get_pos_period")(
        salon_id="biz-un", period_id="per-1"))

    _aucun_champ_interdit(out)
    assert out["initial_eur"] == 50.0 and out["final_eur"] == 95.0
    assert out["receipts"][0]["total_ttc_eur"] == 45.0


def test_une_liste_de_periodes_passe_la_fenetre_en_millisecondes(coeur):
    """Les périodes sont indexées sur `createdAt`, en millisecondes : c'est la
    seule des deux fenêtres du connecteur qui ne se donne PAS en jours."""
    coeur.client.list_pos_periods.return_value = []
    asyncio.run(_outil(coeur, "planity_list_pos_periods")(
        salon_id="biz-un", preset="30d", limit=5))
    coeur.client.list_pos_periods.assert_awaited_once_with(
        "biz-un", 1_000, 2_000, limit=5)


def test_une_periode_introuvable_le_dit(coeur):
    coeur.client.get_pos_period.return_value = None
    out = asyncio.run(_outil(coeur, "planity_get_pos_period")(
        salon_id="biz-un", period_id="inconnue"))
    assert out["error"] == "not_found"


# ── Supprimé n'est pas absent ───────────────────────────────────────────────

def test_les_collaboratrices_supprimees_sont_ecartees_par_defaut(coeur):
    """Quatre supprimées sur sept : sans ce filtre, le salon en annonce sept."""
    coeur.client.get_salon.return_value = _salon(employees=[
        _employe("emp-1", "Alex"),
        _employe("emp-2", "Camille", deleted_at=1_757_000_000_000),
        _employe("emp-3", "Cabine 1", type_="room", title="Cabine 1")])
    outil = _outil(coeur, "planity_list_employees")

    actives = asyncio.run(outil(salon_id="biz-un"))
    assert [e["id"] for e in actives] == ["emp-1", "emp-3"]

    toutes = asyncio.run(outil(salon_id="biz-un", include_deleted=True))
    assert [e["id"] for e in toutes] == ["emp-1", "emp-2", "emp-3"]
    partie = [e for e in toutes if e["id"] == "emp-2"][0]
    assert partie["deleted"] is True
    assert partie["deleted_at"] == _iso(1_757_000_000_000)


def test_un_enfant_d_agenda_qui_n_est_pas_une_personne_le_dit(coeur):
    """Une cabine, un poste, une ressource : `type` et `title` sortent tels que
    Planity les stocke, sans qu'on leur invente un sens."""
    coeur.client.get_salon.return_value = _salon(employees=[
        _employe("emp-3", "Cabine 1", type_="room", title="Cabine 1")])
    e = asyncio.run(_outil(coeur, "planity_list_employees")(salon_id="biz-un"))[0]
    assert (e["type"], e["title"]) == ("room", "Cabine 1")


def test_la_fiche_salon_compte_les_supprimees_sans_les_lister(coeur):
    coeur.client.get_salon.return_value = _salon(employees=[
        _employe("emp-1", "Alex"),
        _employe("emp-2", "Camille", deleted_at=7)])
    out = asyncio.run(_outil(coeur, "planity_get_salon_info")(salon_id="biz-un"))
    assert [e["id"] for e in out["employees"]] == ["emp-1"]
    assert out["employees_deleted_count"] == 1


@exige_les_sous_modules
def test_un_produit_rend_ses_lots_et_ses_seuils_non_renseignes(coeur):
    """Le prix d'achat vit dans le LOT : l'écraser en un total fait disparaître la
    marge. Et un seuil absent vaut `null`, pas `0` — `0` commanderait tout."""
    coeur.client.list_products.return_value = {"cat-1": {"children": {
        "prd-1": {"name": "Shampooing", "price": 1500, "stocks": {
            "lot-1": {"quantity": 4, "purchasePrice": 700, "createdAt": 2},
            "lot-2": {"quantity": 3, "purchasePrice": 800, "createdAt": 1}}}}}}
    p = asyncio.run(_outil(coeur, "planity_list_products")(salon_id="biz-un"))[0]

    assert p["price_eur"] == 15.0 and p["stock_total"] == 7
    assert [l["purchase_price_eur"] for l in p["stock_lots"]] == [8.0, 7.0]
    assert p["stock_threshold"] is None and p["stock_ceiling"] is None
    assert p["supplier_id"] is None


@exige_les_sous_modules
def test_les_produits_supprimes_sont_ecartes_par_defaut(coeur):
    coeur.client.list_products.return_value = {"cat-1": {"children": {
        "prd-1": {"name": "Vivant"},
        "prd-2": {"name": "Retiré", "deletedAt": 7}}}}
    outil = _outil(coeur, "planity_list_products")
    assert [p["id"] for p in asyncio.run(outil(salon_id="biz-un"))] == ["prd-1"]
    tous = asyncio.run(outil(salon_id="biz-un", include_deleted=True))
    assert sorted(p["id"] for p in tous) == ["prd-1", "prd-2"]


# ── La fenêtre se dit ───────────────────────────────────────────────────────

def test_une_fenetre_qui_finit_aujourd_hui_le_dit(coeur):
    """C'est ce qui empêche « au rythme actuel, il reste N jours » de sortir faux :
    le dernier jour n'est pas fini, donc le rythme journalier est sous-estimé."""
    from oto_mcp.tools import planity_session

    maintenant = int(time.time() * 1000)
    p = planity_session.periode(maintenant - 6 * 86_400_000, maintenant)
    assert p["ends_today"] is True and p["complete"] is False
    assert p["days"] == 7 and p["timezone"] == "Europe/Paris"


def test_une_fenetre_close_est_annoncee_complete(coeur):
    from oto_mcp.tools import planity_session

    hier = int(time.time() * 1000) - 86_400_000
    p = planity_session.periode(hier - 86_400_000, hier)
    assert p["ends_today"] is False and p["complete"] is True


def test_le_ca_jour_par_jour_porte_sa_periode_et_compte_ses_jours_servis(coeur):
    """Les jours sans encaissement sont ABSENTS de la série, pas à zéro : lire
    `len(days)` comme une durée fait rater les jours de fermeture."""
    coeur.client.get_revenues.return_value = {"all": {
        "1000": {"revenueWithVAT": 12_000, "revenueWithoutVAT": 10_000,
                 "quantity": 3}}}
    out = asyncio.run(_outil(coeur, "planity_get_daily_revenue")(
        salon_id="biz-un", preset="7d"))

    assert out["days_with_revenue"] == 1
    assert out["days"][0]["revenue_ttc_eur"] == 120.0
    assert out["days"][0]["date"] == _iso(1_000)[:10]
    assert out["period"]["timezone"] == "Europe/Paris"
    assert "complete" in out["period"]


def test_le_ca_par_moyen_de_paiement_totalise_et_ne_convertit_pas_ce_qu_il_ignore(coeur):
    """`revenue` est de l'argent ; `amount` ne l'est pas et son sens n'est pas
    établi — lui coller un signe euro le ferait lire comme un encaissement."""
    coeur.client.get_revenue_by_payment_method.return_value = {
        "1000": {"creditCard": {"revenue": 12_000, "amount": 3,
                                "paymentMethodName": "CB"}},
        "2000": {"creditCard": {"revenue": 3_000, "amount": 1,
                                "paymentMethodName": "CB"},
                 "cash": {"revenue": 1_000, "amount": 1,
                          "paymentMethodName": "Espèces"}},
        "3000": {},
    }
    out = asyncio.run(_outil(coeur, "planity_get_revenue_by_payment_method")(
        salon_id="biz-un", preset="30d"))

    par_methode = {m["method"]: m for m in out["by_method"]}
    assert par_methode["creditCard"]["revenue_eur"] == 150.0
    assert par_methode["cash"]["revenue_eur"] == 10.0
    assert out["by_method"][0]["method"] == "creditCard", "non classé par montant"
    assert len(out["by_day"]) == 3, "un jour sans encaissement doit rester visible"
    jour = out["by_day"][0]["methods"][0]
    assert jour["amount"] == 3 and "amount_eur" not in jour


# ── Le stock ────────────────────────────────────────────────────────────────

def test_les_mouvements_passent_les_produits_demandes_tels_quels(coeur):
    coeur.client.list_stock_movements.return_value = [
        {"product_id": "prd-1", "id": "m-1", "created_at": 1_000, "type": "sale",
         "quantity": -1, "purchase_price_cents": 700,
         "purchase_price_raw": 700, "motive": None}]
    out = asyncio.run(_outil(coeur, "planity_list_stock_movements")(
        salon_id="biz-un", product_ids=["prd-1"], preset="90d"))

    coeur.client.list_stock_movements.assert_awaited_once_with(
        "biz-un", ["prd-1"], 1_000, 2_000)
    assert out["products_scanned"] == 1 and out["count"] == 1
    assert out["movements"][0]["purchase_price_eur"] == 7.0
    assert out["movements"][0]["type"] == "sale"
    coeur.client.list_products.assert_not_awaited()


@exige_les_sous_modules
def test_un_balayage_de_tout_le_catalogue_est_refuse_en_nommant_le_chemin_court(coeur):
    """Les mouvements se lisent UN PRODUIT À LA FOIS. Balayer neuf cents références
    tiendrait la conversation une demi-minute : on refuse en disant quoi faire à la
    place, plutôt que de tronquer — une liste de ventes tronquée se lit comme un
    produit qui ne se vend plus."""
    coeur.client.list_products.return_value = {"cat-1": {"children": {
        f"prd-{i}": {"name": f"P{i}"} for i in range(5)}}}
    with pytest.raises(McpError) as e:
        asyncio.run(_outil(coeur, "planity_list_stock_movements")(
            salon_id="biz-un", max_products=3))
    message = str(e.value)
    assert "product_ids" in message and "max_products=5" in message
    assert "planity_get_revenue_breakdown" in message


@exige_les_sous_modules
def test_sous_la_borne_le_catalogue_vivant_est_balaye(coeur):
    """Et les produits SUPPRIMÉS n'en sont pas : ils ne se réassortissent pas."""
    coeur.client.list_products.return_value = {"cat-1": {"children": {
        "prd-1": {"name": "Vivant"}, "prd-2": {"name": "Retiré", "deletedAt": 7}}}}
    coeur.client.list_stock_movements.return_value = []
    out = asyncio.run(_outil(coeur, "planity_list_stock_movements")(
        salon_id="biz-un"))
    coeur.client.list_stock_movements.assert_awaited_once_with(
        "biz-un", ["prd-1"], 1_000, 2_000)
    assert out["products_scanned"] == 1


def test_un_fournisseur_absent_est_une_reponse_pas_une_panne(coeur):
    """Un salon qui commande par téléphone n'en déclare aucun : rendre une liste
    vide est la bonne réponse, et il n'y a rien à réessayer."""
    coeur.client.list_suppliers.return_value = []
    out = asyncio.run(_outil(coeur, "planity_list_suppliers")(salon_id="biz-un"))
    assert out == {"count": 0, "suppliers": []}


def test_les_commandes_rendent_leur_curseur_de_page(coeur):
    coeur.client.list_product_orders.return_value = {"data": [{"id": "c-1"}],
                                                     "cursor": "suite"}
    out = asyncio.run(_outil(coeur, "planity_list_product_orders")(
        salon_id="biz-un"))
    assert out["count"] == 1 and out["cursor"] == "suite"
    coeur.client.list_product_orders.assert_awaited_once_with("biz-un", cursor=None)


def test_les_sorties_de_masse_sont_datees(coeur):
    """Un stock qui baisse sans vente est souvent là : ne regarder que les ventes
    rend l'écart inexplicable."""
    coeur.client.list_mass_stock_removals.return_value = [
        {"id": "s-1", "created_at": 1_000, "products_count": 3, "products": {}}]
    out = asyncio.run(_outil(coeur, "planity_list_mass_stock_removals")(
        salon_id="biz-un"))
    assert out["removals"][0]["date"] == _iso(1_000)
    assert out["removals"][0]["products_count"] == 3
