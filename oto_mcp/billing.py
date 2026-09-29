"""Billing par org (ADR 0043) — abonnement unique, PSP Mollie.

⚠️ **Depuis la coupure du cœur (#1097), ce module ne pose plus aucun droit.** La
facturation est tenue par oto-commerce, qui pose seul les droits déclarés
(`org_entitlements`) par l'API de service. Les gestes servis qui vendaient ou offraient
(`subscribe`, `confirm`, changement de moyen de paiement, résiliation et reprise, plan
offert, contrat) sont refusés en 409 `billing_moved` à la capacité
(`capabilities/_facturation_externe.py`) ; `confirm` lui-même, encore atteint par le
webhook et le rattrapage du runner, grave l'encaissement puis refuse (`billing_moved`,
une erreur au journal) sans ouvrir d'abonnement ; le runner refuse de prélever
(`billing_runner`). Ce qui suit reste pour les lectures (`status`, factures) et le
webhook Mollie ; le reste est l'histoire du cycle d'avant.

Le cycle est piloté ICI (miroir local `org_subscriptions` = source de vérité,
PSP-agnostique par conception ADR 0043) :
- `subscribe` ouvre le PREMIER paiement sur la page de checkout hébergée Mollie
  (`sequenceType=first` — 3DS carte ou collecte IBAN + mandat SEPA gérés par eux,
  UN seul flux) et journalise le paiement ;
- `confirm` LIT le paiement au retour du payeur (et en réconciliation) : encaissé
  (`paid`) → journalise l'encaissement AUSSITÔT (#493), puis, depuis la coupure du
  cœur, refuse (`billing_moved`) sans poser de miroir ;
- la résiliation et sa reprise (`cancel`, `resume`) ont été retirées à la coupure du
  cœur : l'abonnement se gère dans oto-commerce.

Bascule Stancer→Mollie (ADR 0043, amende 2026-07-24) : Mollie **unifie carte et
SEPA** derrière un customer + un mandat créé au premier paiement → plus de chemin
SEPA séparé (IBAN tokenisé + signature OTP + ICS créancier). Le rejeu MIT tire sur
`customerId`+`mandateId`. Webhooks natifs (barreau ultérieur) ; polling = socle.

Le plan (prix, options débloquées) vit dans `PLANS` — mapping en CODE (pas de
table) : la vérité produit est versionnée et relue par l'entitlement (has_option,
2e source). ⚠️ Valeurs actuelles = prix actés Alexis 2026-07-06, **HORS TAXES**.

⚠️ **Le montant DÉBITÉ est le TTC** depuis #486 : le prix du palier est un HT, et
le taux dépend du pays du payeur (`billing_vat`). D'où l'ordre imposé — identité
de facturation d'abord, paiement ensuite : `subscribe` refuse tant que l'org n'a
pas dit qui paie et depuis quel pays, parce que sans cela le montant à prendre
n'est pas connu. Le calcul est fait par UN seul seam (`tax_for_org`), partagé
avec l'échéance du `billing_runner`.

⚠️ **Souscrire demande un CONSENTEMENT, pas seulement un moyen de paiement**
(#487) : `subscribe` refuse aussi tant que l'appelant n'a pas accepté les
documents du contexte `purchase` (CGU + CGV + DPA) à leur version courante. Sans
acceptation horodatée, les CGV et le DPA ne sont opposables à personne. Les deux
préalables sont évalués ENSEMBLE et rendus d'un coup (`_purchase_preconditions`) :
un tunnel qui les découvre l'un après l'autre fait remplir un formulaire pour
opposer une case à cocher au clic suivant.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import billing_consent, billing_grants, billing_vat, config, mollie_client
from . import billing_mode
from . import db
from .db import billing as db_billing
# Le format de date servi par l'API est défini UNE fois, dans la couche DB (le row
# factory normalise tout datetime relu). Une réponse qui construit sa date sans
# passer par là fabrique un second format pour le même champ — cf. #291.
from .db._conn import _normalize_value

logger = logging.getLogger(__name__)

# Le mandat réutilisable ne naît pas AVEC l'encaissement : chez Mollie il apparaît
# quelques minutes après (1,4 s après le paiement du 25/08, il n'existait pas ;
# visible à +5 min). Cette fenêtre est donc la durée pendant laquelle une
# souscription est « en vol » — pendant laquelle un mandat absent est une COURSE et
# non un refus, et pendant laquelle ouvrir un second checkout ne peut que débiter
# deux fois (#493).
PENDING_WINDOW = timedelta(minutes=30)
# Nom du paramètre qui porte l'identité du paiement sur l'URL de retour navigateur.
RETURN_REF_PARAM = "payment_ref"


def is_enabled() -> bool:
    """Feature flag global (ADR 0043, dark launch) : la surface billing (capacités
    REST/MCP + nav dashboard + runner) n'est exposée QUE si `OTO_BILLING_ENABLED=1`.
    Absent/0 = dormant. Piloté par-déploiement (prod off tant que le PSP n'est pas
    live, canari on) sans divergence de branche ni revert."""
    return os.environ.get("OTO_BILLING_ENABLED", "0") == "1"

# plan → prix (centimes), intervalle, options de connecteur débloquées (couche 3,
# lues par access.has_option). Prix HT mensuels : 19/99/249/499 (Alexis 2026-08-03,
# 2ᵉ palier 49 → 99 le 2026-08-29, #490 — personne n'y était abonné, pas de rétroactif).
# Chaque plan CONFIGURE l'org à l'activation → une seule action admin.
# **Modèle simplifié (2026-08-03)** : gratuit = pas d'Unipile (option bloquée, sauf
# `comp` admin = « offert ») ; payant = Unipile + clés plateforme SANS quota
# (`unmetered`). On NE facture PLUS au nombre de comptes messagerie → tous les
# paliers ont `unipile_accounts=None`, qui veut dire « le plan n'a PAS d'avis sur les
# sièges » (arbitrage du 23/09, #805) — surtout pas « illimité » : le plafond en place
# (posé à la main, ou le défaut plateforme de 5) reste ce qu'il est. ⚠️ Les 4 paliers
# débloquent donc
# AUJOURD'HUI exactement la même chose et ne diffèrent QUE par le prix — la
# différenciation (« payant = Unipile, mais pas que ») viendra plus tard (options
# premium par palier). `unmetered=True` = fin des credits d'appel.
PLANS: dict[str, dict] = {
    "standard": {
        "label": "Standard", "amount": 1900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
    "premium": {
        "label": "Premium", "amount": 9900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
    "business": {
        "label": "Business", "amount": 24900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
    "enterprise": {
        "label": "Entreprise", "amount": 49900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": None, "unmetered": True,
    },
}


def plans() -> list[dict]:
    """Catalogue public (l'UI billing du dashboard boucle dessus)."""
    return [{"plan": k, "custom": v.get("custom", False),
             **{f: v[f] for f in ("label", "amount", "currency", "interval",
                                  "unipile_accounts")}}
            for k, v in PLANS.items()]


def plan_options(plan: str) -> frozenset[str]:
    """Options de connecteur débloquées par `plan` (consommé par access.has_option)."""
    meta = PLANS.get(plan)
    return frozenset(meta["options"]) if meta else frozenset()


def plan_is_unmetered(plan: str) -> bool:
    """Le plan lève-t-il les quotas des clés plateforme ? (fin des credits d'appel)."""
    meta = PLANS.get(plan)
    return bool(meta and meta.get("unmetered"))


def plan_rights(plan: str) -> tuple[str, ...]:
    """Les droits déclarés (`org_entitlements.right_key`) qu'un plan ouvre : ses options,
    plus la levée du quota plateforme s'il est `unmetered`. Plan inconnu = rien. Lu par
    l'export de reprise d'oto-commerce (`service_commerce`) : le cœur ne les pose plus."""
    from .access.entitlements import PLATFORM_UNMETERED
    droits = tuple(sorted(plan_options(plan)))
    return droits + ((PLATFORM_UNMETERED,) if plan_is_unmetered(plan) else ())


def _add_period(dt: datetime, interval: str) -> datetime:
    """Échéance suivante au mois/an CALENDAIRE (pas d'approximation 30 j) —
    borné au dernier jour du mois cible (31/01 + 1 mois → 28/02)."""
    if interval == "year":
        return _safe_replace(dt, year=dt.year + 1, month=dt.month)
    month = dt.month + 1
    year = dt.year + (1 if month > 12 else 0)
    return _safe_replace(dt, year=year, month=((month - 1) % 12) + 1)


def _safe_replace(dt: datetime, *, year: int, month: int) -> datetime:
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def webhook_url() -> str:
    """URL publique que Mollie rappelle à chaque changement d'état d'un paiement
    (base = `OTO_MCP_PUBLIC_URL`, cf. Logto/Google OAuth). Portée par chaque
    paiement créé → réconciliation événementielle en complément du polling."""
    return f"{config.public_base_url()}/api/billing/webhook"


# ── TVA : le seam unique entre l'identité de l'org et un montant ─────────────

def tax_for_org(org_id: int, amount_ht: int) -> dict:
    """La décomposition fiscale d'un montant HT pour CETTE org, au moment du débit.

    Un seul seam pour les deux chemins qui débitent — `subscribe` (premier paiement)
    et `billing_runner._charge_one` (échéance). Deux calculs auraient divergé au
    premier changement de règle, et la divergence se verrait sur une facture, pas
    dans un test.

    Lève `billing_identity_required` (identité absente/incomplète) ou
    `vat_consumer_unsupported` (particulier de l'Union hors France) : dans les deux
    cas, il n'y a pas de montant correct à prendre, et prendre le HT « en attendant »
    est exactement ce que #486 répare."""
    return billing_vat.tax_for_identity(
        amount_ht, db_billing.get_billing_identity(org_id))


# ── les préalables de la souscription ────────────────────────────────────────

def _purchase_preconditions(org_id: int, sub: Optional[str],
                            amount_ht: int) -> tuple[Optional[dict], list[dict]]:
    """Les DEUX préalables d'un achat, évalués ensemble. Rend `(décomposition
    fiscale, manques)` — la décomposition est `None` dès que l'identité manque.

    **L'ordre est celui du tunnel : identité, puis légal.** Il n'est pas cosmétique.
    Le payeur accepte des CGV *pour un montant*, et le montant n'existe qu'une fois
    le pays connu (c'est lui qui décide de la TVA, #486). Faire consentir d'abord et
    chiffrer ensuite ferait accepter un prix qui n'a pas encore été annoncé — le
    consentement est le DERNIER geste avant la page de paiement.

    Mais ordonner n'est pas refuser un à la fois : les deux manques partent
    ENSEMBLE, et c'est ce qui permet au tunnel de peindre l'écran entier — le
    formulaire d'identité ET les trois cases — en un seul aller-retour.

    Aucun effet de bord : rien n'est créé chez le PSP tant que cette liste n'est pas
    vide. Un refus après création laisserait derrière lui un customer et une page
    payable."""
    manques: list[dict] = []
    tax: Optional[dict] = None
    try:
        tax = tax_for_org(org_id, amount_ht)
    except ValueError as e:
        message = str(e)
        manques.append({"code": message.split(":", 1)[0].strip(), "message": message})
    legal = billing_consent.legal_blocker(sub)
    if legal:
        manques.append(legal)
    return tax, manques


# ── souscription ─────────────────────────────────────────────────────────────

def _elapsed_since(value, now: datetime) -> Optional[timedelta]:
    """Temps écoulé depuis un horodatage, quelle que soit sa forme.

    Le journal est relu NORMALISÉ par le row factory (« YYYY-MM-DD HH:MM:SS », sans
    fuseau, donc UTC implicite) ; Mollie, lui, rend de l'ISO 8601 à offset (parfois
    suffixé `Z`, que `fromisoformat` ne lit pas avant 3.11). `None` = horodatage
    illisible ou absent — l'appelant décide, il ne devine pas.
    """
    if isinstance(value, datetime):
        return now - (value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # noqa: SILENT — horodatage illisible : on rend None, et l'appelant tranche
        except ValueError:
            return None
        return now - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    return None


def _age_label(age: Optional[timedelta]) -> str:
    if age is None:
        return "à l'instant"
    s = int(age.total_seconds())
    return f"{s} s" if s < 120 else f"{s // 60} min"


def _return_url_with_ref(return_url: str, payment_id: str) -> str:
    """Ajoute `?payment_ref=tr_…` à l'URL de retour du navigateur (en écrasant une
    valeur déjà posée), sans toucher au reste de la query string du dashboard."""
    parts = urlparse(return_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != RETURN_REF_PARAM]
    query.append((RETURN_REF_PARAM, payment_id))
    return urlunparse(parts._replace(query=urlencode(query)))


def _payment_pending_message(in_flight: dict, now: datetime) -> str:
    """Le refus dit QUI occupe la place, depuis quand, et ce qu'il reste à faire —
    le remède n'est pas le même selon que l'argent est déjà pris ou non."""
    ref = in_flight.get("payment_intent_id") or f"ligne {in_flight.get('id')}"
    statut = in_flight.get("status")
    age = _age_label(_elapsed_since(in_flight.get("created_at"), now))
    suite = (
        "il est DÉJÀ ENCAISSÉ : l'abonnement s'ouvre seul dès que le mandat est "
        "disponible chez le PSP, quelques minutes plus tard"
        if statut == "paid" else
        "sa page de paiement est encore payable : la terminer ou la laisser expirer"
    )
    return (f"payment_pending: une souscription est déjà en cours pour cette org "
            f"(paiement {ref}, statut {statut}, ouvert il y a {age}) — {suite}. "
            f"Ouvrir un second paiement débiterait deux fois.")


def _org_customer_id(org_id: int, existing: Optional[dict]) -> Optional[str]:
    """Le customer Mollie de l'org — un seul, pour toujours (#493).

    Il se lit sur le miroir quand il existe, SINON sur le journal : le miroir n'est
    posé qu'à `confirm`, donc au deuxième clic d'une souscription en cours il n'y a
    encore rien à relire. C'est là qu'un second customer naissait, avec son propre
    mandat — celui que le rejeu MIT ne tirera jamais."""
    if existing and existing.get("customer_id"):
        return existing["customer_id"]
    return db_billing.last_customer_id_for_org(org_id)


def subscribe(org_id: int, plan: str, return_url: str, *, sub: Optional[str],
              method: str = "card") -> dict:
    """Ouvre la souscription. UN seul flux (Mollie unifie carte et SEPA) : premier
    paiement `sequenceType=first` → l'URL renvoyée = la page de checkout hébergée
    Mollie où le payeur finit le geste (3DS carte, ou saisie IBAN + acceptation du
    mandat SEPA). `method` ∈ {card, sepa} restreint la page ; le mandat réutilisable
    naît à l'encaissement. Le miroir n'est PAS posé ici — il naît à `confirm`
    (paiement constaté), qui relit le plan de la `metadata` du paiement.

    **Une seule souscription en vol à la fois** (#493) : tant qu'un premier paiement
    de moins de `PENDING_WINDOW` n'a pas définitivement échoué, un second checkout
    est refusé (`payment_pending`). C'est le geste le plus banal du monde — payer,
    voir un échec, recliquer — et c'est lui qui a débité 38 € un abonnement à 19 €.
    Corollaire assumé : résilier puis re-souscrire dans la demi-heure est refusé le
    temps que la fenêtre s'écoule, le refus disant quel paiement l'occupe.

    `sub` = l'appelant, et il est OBLIGATOIRE : c'est une personne qui accepte des
    documents, pas une organisation. Sans lui rien n'est accepté et la souscription
    est refusée — le paramètre n'a pas de défaut pour que l'oubli soit une erreur de
    programmation, jamais un gate ouvert."""
    meta = PLANS.get(plan)
    if meta is None:
        raise ValueError(f"unknown_plan: {plan!r} (plans : {', '.join(PLANS)})")
    if meta.get("custom"):
        raise ValueError("custom_plan: ce palier est sur devis — contacter "
                         "Otomata (un admin l'active en abonnement comp)")
    if method not in ("card", "sepa"):
        raise ValueError(f"unknown_method: {method!r} (card | sepa)")
    existing = db_billing.get_org_subscription(org_id)
    # Un contrat arrivé à sa date de fin ne tient plus la place : l'org peut souscrire.
    contrat_fini = (existing and existing.get("provider") == "contract"
                    and (db_billing.get_contract(org_id) or {}).get("ended"))
    if (existing and existing["status"] == "active" and not existing.get("canceled_at")
            and not contrat_fini):
        raise ValueError("already_subscribed: l'org a déjà un abonnement actif")

    now = datetime.now(timezone.utc)
    in_flight = db_billing.pending_initial_payment(org_id, since=now - PENDING_WINDOW)
    if in_flight:
        raise ValueError(_payment_pending_message(in_flight, now))

    # LES PRÉALABLES AVANT LE CHECKOUT (#486 pour l'identité, #487 pour le légal).
    # Le prix du palier est un HT ; ce qui part au PSP est le TTC, et le taux dépend
    # du pays du payeur. Et on ne vend pas sans consentement écrit. Les deux se
    # tranchent donc AVANT de créer quoi que ce soit chez Mollie : un refus après
    # création laisserait un customer et une page payable derrière lui.
    tax, manques = _purchase_preconditions(org_id, sub, meta["amount"])
    if manques:
        raise billing_consent.PurchaseBlocked(manques)

    customer_id = _org_customer_id(org_id, existing)
    if not customer_id:
        cust = mollie_client.create_customer(
            name=f"Otomata org {org_id}", metadata={"org_id": str(org_id)})
        customer_id = cust["id"]

    payment = mollie_client.create_first_payment(
        tax["amount_ttc"], customer_id=customer_id, currency=meta["currency"],
        redirect_url=return_url, description=f"Abonnement {meta['label']}",
        method=mollie_client.mollie_method(method), webhook_url=webhook_url(),
        # le plan voyage dans la metadata du paiement (pas d'état serveur pendant
        # le checkout : confirm le relit → survit à un restart).
        metadata={"org_id": str(org_id), "plan": plan})
    db_billing.insert_billing_payment(
        org_id, "initial", tax["amount_ttc"], currency=meta["currency"],
        payment_intent_id=payment["id"], status=payment.get("status", "open"),
        customer_id=customer_id, tax=tax)
    # Le retour navigateur doit DIRE quel paiement il vient de conclure. Mollie
    # n'ajoute rien à `redirectUrl`, et cette URL se fixe à la création — où l'id du
    # paiement n'existe pas encore : on la ré-écrit donc juste après, le paiement
    # étant encore ouvert. Un refus de Mollie ne casse pas un checkout payable pour
    # un confort : `confirm` retombe sur le plus récent non conclu et le webhook,
    # lui, connaît toujours l'identité du paiement — mais on le DIT.
    try:
        mollie_client.update_payment(
            payment["id"],
            redirect_url=_return_url_with_ref(return_url, payment["id"]))
    except mollie_client.MollieError as e:
        logger.warning("billing: URL de retour non datée du paiement %s (org %s) — "
                       "le retour navigateur devra deviner : %s",
                       payment["id"], org_id, e)
    return {"checkout_url": mollie_client.checkout_url(payment),
            "payment_intent_id": payment["id"], "plan": plan, "method": method,
            # La décomposition part AVEC la réponse : le tunnel doit pouvoir annoncer
            # « 19,00 € HT + 3,80 € de TVA = 22,80 € » avant d'envoyer sur la page de
            # checkout, sinon le payeur découvre le TTC chez Mollie.
            "amount_ht": tax["amount_ht"], "vat_rate_bps": tax["vat_rate_bps"],
            "vat_amount": tax["vat_amount"], "amount_ttc": tax["amount_ttc"],
            "vat_scheme": tax["vat_scheme"], "vat_mention": tax["vat_mention"]}


def confirm(org_id: int, payment_ref: Optional[str] = None) -> dict:
    """Constate un premier paiement non conclu : échoué → journalisé ; pas encore
    encaissé → `pending` ; encaissé (`paid`) → l'encaissement est journalisé et tracé,
    puis **refusé** (`billing_moved`, erreur au journal nommant l'org et le paiement) :
    depuis la coupure du cœur (#1097), aucun abonnement ne s'ouvre ici, oto-commerce
    tient la facturation. Re-confirmer un abonnement déjà actif reste un no-op
    informatif.

    `payment_ref` = l'identifiant du paiement à traiter, quand l'appelant le
    connaît. Le **webhook** le connaît (c'est celui qu'il vient de recevoir) et
    DOIT le passer ; le **retour navigateur** le porte désormais aussi (#493, il est
    daté sur l'URL de retour) ; le **polling** ne le connaît pas et prend le plus
    récent, ce qui reste correct pour lui. Sans ce paramètre, l'identité du paiement
    encaissé se perdait entre le webhook et ce chemin (#291)."""
    sub_row = db_billing.get_org_subscription(org_id)
    # Idempotence D'ABORD. Elle tenait jusqu'ici au fait qu'un paiement confirmé
    # sortait de la file (`paid` = terminal) ; depuis #493 un encaissement RESTE
    # candidat tant que le miroir n'est pas posé, donc sans ce garde-fou explicite
    # re-confirmer un abonnement ouvert repousserait `current_period_end` d'une
    # période à chaque appel. Un abonnement résilié (canceled_at) n'est pas concerné :
    # il peut légitimement re-souscrire, exactement comme dans `subscribe`.
    if sub_row and sub_row["status"] == "active" and not sub_row.get("canceled_at"):
        return {"status": "active", "plan": sub_row["plan"]}

    # Candidats = les premiers paiements qui n'ont pas DÉFINITIVEMENT échoué. `paid`
    # en fait partie (#493) : l'encaissement est journalisé dès son constat, avant le
    # contrôle de mandat, donc un paiement réussi dont l'abonnement reste à ouvrir se
    # présente ici avec un statut terminal. Seuls failed/canceled/expired sortent.
    failed = set(db_billing.TERMINAL_PAYMENT_STATUSES) - {"paid"}
    candidates = [
        # ⚠️ `limit` explicite : au défaut (20), un `initial` ouvert plus ancien
        # devenait carrément INVISIBLE dès qu'une org avait vingt lignes de paiement
        # — donc jamais confirmé, sans le moindre message.
        p for p in db_billing.list_billing_payments(org_id, limit=200)
        if p["kind"] == "initial"
        and p["status"] not in failed
        and p.get("payment_intent_id")
    ]
    if not candidates:
        if sub_row and sub_row["status"] == "active":
            return {"status": "active", "plan": sub_row["plan"]}
        raise ValueError("no_pending_subscription: aucun paiement initial en cours")

    # Rien n'interdit deux souscriptions ouvertes à la fois (retour arrière, page
    # rechargée, hésitation carte/SEPA) : il peut donc exister PLUSIEURS paiements
    # ouverts, chacun avec une page payable. Quand l'appelant sait lequel a été
    # encaissé — le webhook le sait, il vient de le recevoir — on traite CELUI-LÀ.
    # Sans ça, le payeur qui termine l'ANCIENNE page était débité pendant que
    # `confirm` regardait la plus récente, la trouvait non payée, et rendait
    # `pending` : encaissé, aucun droit ouvert, aucune erreur nulle part.
    if payment_ref:
        row = next((p for p in candidates if p["payment_intent_id"] == payment_ref), None)
        if row is None:
            # Le paiement visé n'est pas (ou plus) un initial en cours de cette org :
            # on ne se rabat PAS sur un autre, ce serait confirmer sur la foi d'un
            # encaissement qui concerne autre chose.
            raise ValueError(
                f"unknown_payment: le paiement {payment_ref} n'est pas un paiement "
                "initial en cours pour cette org")
    else:
        row = candidates[0]  # le plus récent (list_billing_payments trie DESC)
    payment = mollie_client.get_payment(row["payment_intent_id"])
    # Le MODE d'abord, avant toute écriture : un paiement de test, ou tout paiement
    # constaté hors production, n'ouvre aucun droit sur la base partagée (`billing_mode`).
    billing_mode.exiger_paiement_reel(payment)
    pstatus = str(payment.get("status") or "")

    if pstatus in ("failed", "canceled", "expired"):
        db_billing.update_billing_payment(row["id"], status=pstatus)
        return {"status": "failed", "payment_status": pstatus, **billing_vat.tax_view(row)}
    if pstatus != "paid":
        # pas encaissé : le payeur est peut-être encore sur la page de checkout.
        return {"status": "pending", "payment_status": pstatus, **billing_vat.tax_view(row)}

    # ENCAISSÉ. On le grave AVANT tout le reste (#493) : le journal doit dire ce que
    # le PSP a fait, pas ce que nous avons su en faire. Le statut n'était écrit
    # qu'après le mandat, le plan et la pose du miroir — un paiement réellement
    # débité restait donc `open` au journal dès que l'une de ces étapes échouait, et
    # `subscribe` ne se gardait sur rien.
    db_billing.update_billing_payment(row["id"], status="paid",
                                      payment_id=payment["id"])
    # La trace suit l'ENCAISSEMENT, pas l'ouverture des droits (#488) : elle est
    # due même si le mandat n'est pas né et que le miroir n'est pas encore posé —
    # sinon le cas du 25/08 (argent pris, abonnement pas ouvert) resterait sans
    # document. ⚠️ Elle TRACE, elle n'émet plus : depuis le 2026-09-09 aucune pièce
    # n'est créée chez Pennylane sans geste humain (`billing_invoices/emission.py`).
    from . import billing_invoices as factures     # import tardif
    factures.tracer_encaissement(row["id"])

    # Coupure du cœur (#1097) : un checkout ouvert AVANT la bascule et payé APRÈS arrive
    # ici. L'encaissement est gravé et tracé ci-dessus — le journal dit ce que le PSP a
    # fait —, mais aucun abonnement n'est ouvert : il n'ouvrirait aucun droit
    # (oto-commerce les pose seul), et le cœur noterait un abonné actif sans droit. Une
    # ERREUR, pour que l'encaissement orphelin se voie et soit repris.
    logger.error("billing: org %s — paiement %s ENCAISSÉ après la coupure du cœur : aucun "
                 "abonnement ouvert, aucun droit (billing_moved) — encaissement orphelin "
                 "à reprendre par oto-commerce", org_id, payment["id"])
    raise ValueError(
        f"billing_moved: paiement {payment['id']} encaissé, mais la facturation est tenue "
        "par le service de facturation (oto-commerce) : aucun abonnement n'est ouvert ici")


# ── état ─────────────────────────────────────────────────────────────────────

def status(org_id: int) -> dict:
    """État d'abonnement de l'org — **et ce qui lui est offert sans abonnement**.

    `granted` porte les avantages payants OFFERTS (dons d'option, cf.
    `billing_grants`). Il est joint dans les DEUX branches, et c'est le point : la
    branche « aucun abonnement » est justement celle où un bénéficiaire se voyait
    vendre ce qu'il possédait déjà. Le catalogue `plans` y reste servi — un don n'est
    pas un abonnement, et la voie pour en prendre un ne doit pas se refermer. Les
    dons faits à une PERSONNE n'y figurent pas : ils n'ouvrent plus d'option payante.
    """
    granted = billing_grants.granted_benefits(org_id)
    # L'usage est servi à TOUT LE MONDE, gratifié ou non, abonné ou non : c'est le
    # seul élément de cet écran qui vaut pour tous les comptes.
    usage = billing_grants.monthly_usage(org_id)
    row = db_billing.get_org_subscription(org_id)
    if not row:
        return {"subscribed": False, "plans": plans(),
                "granted": granted, "usage": usage}
    meta = PLANS.get(row["plan"], {})
    comp = row["provider"] == "comp"   # abonnement forcé par un admin (non payé)
    # Abonnement réglé HORS PLATEFORME : payé ailleurs, rien n'est prélevé ici.
    ligne = db_billing.get_contract(org_id) if row.get("provider") == "contract" else None
    contrat = ({k: ligne.get(k) for k in ("seats", "unit_amount", "currency", "interval",
                                          "starts_at", "ends_at", "reference")}
               if ligne else None)
    fini = bool(ligne and ligne["ended"])
    return {
        "subscribed": row["status"] in ("active", "past_due") and not fini,
        "plan": row["plan"], "label": meta.get("label"),
        "amount": meta.get("amount"), "currency": meta.get("currency"),
        "interval": meta.get("interval"),
        # Ce que coûtera la PROCHAINE échéance, TVA comprise (#486) : `amount` reste
        # le prix HT du catalogue, et le TTC en est dérivé par l'identité COURANTE de
        # l'org — donc il bouge si l'org déménage, ce qui est le comportement voulu
        # (c'est bien ce qui sera prélevé). Ce qui a DÉJÀ été pris se lit sur
        # billing.payments, qui a figé sa propre décomposition.
        #
        # ⚠️ SAUF sur un abonnement OFFERT : rien n'y sera jamais prélevé, donc il n'y
        # a pas de TTC à annoncer — et poser `vat_blocked` sur une org offerte sans
        # identité de facturation serait une FAUSSE alerte, sur un écran dont c'est
        # tout le rôle de signaler les échéances en danger.
        **(billing_vat.BLANK_PREVIEW if comp or contrat else billing_vat.tax_preview(
            meta.get("amount"), db_billing.get_billing_identity(org_id))),
        "status": row["status"], "method": row["method"],
        "provider": row.get("provider"),
        "comp": comp,
        "contract": contrat,
        "current_period_end": row.get("current_period_end"),
        "next_billing_at": row.get("next_billing_at"),
        "grace_until": row.get("grace_until"),
        "canceled_at": row.get("canceled_at"),
        # Ce que le RUNNER a CONSTATÉ, à distinguer de `vat_blocked` juste au-dessus.
        # `vat_blocked` est une prévision recalculée à chaque lecture (« au taux
        # d'aujourd'hui, on ne saurait pas quoi prélever ») ; `block_code` est un fait
        # daté (« l'échéance du 25 n'a PAS pu être tirée, et depuis on sert sans
        # encaisser »). Un blocage de TVA réparé une heure après l'échéance efface le
        # premier et laisse le second : c'est précisément la différence utile.
        "block_code": row.get("block_code"),
        "block_detail": row.get("block_detail"),
        "block_since": row.get("block_since"),
        # Servis dans les DEUX branches : un abonné a lui aussi des dons possibles
        # (une option offerte survit à une souscription) et un usage à voir.
        "granted": granted,
        "usage": usage,
    }


# ── webhook Mollie (réconciliation événementielle) ───────────────────────────

def process_webhook(payment_id: str) -> str:
    """Traite un rappel webhook Mollie (le corps ne porte QUE l'id du paiement —
    on re-fetch l'objet avec NOTRE clé, jamais de confiance dans le POST). Retourne
    l'issue (log) : 'ignored' | 'confirmed' | 'not_confirmed' | 'updated' |
    'unchanged' | 'refunded'.

    Sécurité : un id inconnu de notre journal est ignoré (un POST forgé ne
    déclenche rien) ; un premier paiement `paid` rejoue `confirm` (idempotent) ;
    sinon on aligne le statut journalisé. Complément du polling (billing_runner),
    pas un remplacement.

    Depuis la coupure du cœur (#1097), un premier paiement encaissé rend
    `not_confirmed` : `confirm` grave l'encaissement puis refuse (`billing_moved`),
    sans ouvrir d'abonnement — l'encaissement orphelin est une erreur au journal."""
    row = db_billing.get_billing_payment_by_ref(payment_id)
    if not row:
        return "ignored"
    payment = mollie_client.get_payment(payment_id)
    status = str(payment.get("status") or "")

    # REMBOURSEMENT (#488). Mollie appelle le MÊME webhook qu'un changement de
    # statut quand un remboursement est créé ou change d'état — les remboursements
    # n'ont pas d'URL à eux (docs.mollie.com/docs/webhooks). Le paiement reste
    # `paid` : c'est `amountRefunded`, absent tant que rien n'est remboursé, qui
    # porte l'information. Traité AVANT le reste, et il conclut : la trace de la
    # facture, elle, a été posée quand le paiement est passé `paid`.
    rembourse = mollie_client.cents_from_amount(payment.get("amountRefunded"))
    if rembourse:
        from . import billing_invoices as factures  # import tardif
        factures.tracer_remboursement(row["id"], rembourse)
        return "refunded"

    if row["kind"] == "initial" and status == "paid":
        # On passe l'identifiant du paiement ENCAISSÉ : sans lui, `confirm` repartait
        # du plus récent et pouvait confirmer un autre paiement — ou rien (#291).
        try:
            out = confirm(row["org_id"], payment_ref=payment_id)
        except (ValueError, RuntimeError) as e:
            # Un encaissement qu'on ne sait pas transformer en droits est un incident
            # à INVESTIGUER, pas une exception à propager : la laisser remonter ferait
            # répondre 500 au webhook, donc relancer Mollie en boucle sur un état que
            # le retry ne réparera pas. On trace fort et on absorbe.
            logger.error("webhook: paiement %s encaissé (org %s) mais NON confirmé — %s",
                         payment_id, row["org_id"], e)
            return "not_confirmed"
        # Et on rend l'issue RÉELLE : annoncer « confirmed » quoi qu'il arrive faisait
        # affirmer au journal le contraire de ce qui s'était passé, ce qui est pire
        # qu'un silence — on cherche l'incident ailleurs.
        if out.get("status") != "active":
            logger.error("webhook: paiement %s encaissé (org %s), abonnement toujours "
                         "%s — investiguer", payment_id, row["org_id"], out.get("status"))
            return "not_confirmed"
        return "confirmed"
    if status and status != row["status"]:
        db_billing.update_billing_payment(row["id"], status=status)
        if status == "paid":
            # Une ÉCHÉANCE encaissée : le premier paiement, lui, passe par `confirm`
            # (branche du dessus), qui trace déjà. Sans cette ligne, une échéance
            # attendrait le tick du runner pour avoir sa ligne.
            from . import billing_invoices as factures   # import tardif
            factures.tracer_encaissement(row["id"])
        return "updated"
    return "unchanged"
