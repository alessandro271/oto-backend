"""Planity — la CAISSE d'un salon : sessions, tickets, moyens de paiement.

Module frère de `planity.py` (référentiel, clientes, agenda) et de
`planity_stats.py` (les agrégats) : même connecteur, même namespace `planity_*`,
même credential, même session (`planity_session`). Ici, le détail ligne à ligne —
ce qui a été vendu, à quel prix, payé comment, par qui.

Une **période** est une session de caisse (ouverture → clôture). Elle porte ses
tickets ; un salon en accumule des centaines, et toute lecture passe par une
fenêtre de dates.

⚠️ **LISTE BLANCHE sur la cliente — exception assumée à « expose le brut ».** Un
ticket Planity porte un instantané COMPLET de la cliente, figé à l'encaissement :
nom, téléphone, email, adresse, commentaire. Rien de tout cela ne sort d'ici. Ce
qui sort, c'est ce qu'on vient chercher dans un ticket — les lignes, les montants,
la TVA, le moyen de paiement, la vendeuse, le statut, le lien vers le rendez-vous —
et pour la cliente un **identifiant**, avec lequel `planity_get_customer` fait le
reste si on le lui demande.

Ce n'est pas un oubli de projection à corriger au nom du parti pris : la cliente
n'est pas dans la conversation, elle n'a rien demandé, et son adresse n'a aucune
raison de traverser un transcript pour répondre « combien j'ai fait hier ».
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .planity_session import _client, _eur_ou_rien, fenetre, iso, periode


def _ticket_public(t: dict) -> dict:
    """Un ticket réduit aux champs qui sortent — la LISTE BLANCHE.

    Écrite une seule fois, au lieu d'une par outil : ce qui protège une cliente ne
    doit pas dépendre de qui recopie quoi. Ce qui n'y figure pas est REFUSÉ, pas
    oublié — `customer` (l'instantané complet) et `raw` en tête."""
    return {
        "id": t["id"],
        "number": t.get("number"),
        "date": iso(t.get("created_at")),
        "operation_type": t.get("operation_type"),
        "seller_id": t.get("seller_id"),
        "seller_name": t.get("seller_name"),
        "customer_id": t.get("customer_id"),
        "lines_count": t.get("lines_count"),
        "lines": [
            {"title": l.get("title"),
             "price_eur": _eur_ou_rien(l.get("price_cents")),
             "unit_price_eur": _eur_ou_rien(l.get("unit_price_cents")),
             "quantity": l.get("quantity"),
             "service_id": l.get("service_id"),
             "product_id": l.get("product_id"),
             "seller_id": l.get("seller_id"),
             "vat_rate": l.get("vat_rate"),
             "vat_code": l.get("vat_code"),
             "from_appointment": l.get("from_appointment")}
            for l in t.get("lines") or []
        ],
        "payments": [
            {"method": p.get("method"),
             "amount_eur": _eur_ou_rien(p.get("amount_cents")),
             "tip_eur": _eur_ou_rien(p.get("tip_cents"))}
            for p in t.get("payments") or []
        ],
        "discount_total_eur": _eur_ou_rien(t.get("discount_total_cents")),
        "total_ttc_eur": _eur_ou_rien(t.get("vat_included_total_cents")),
        "total_ht_eur": _eur_ou_rien(t.get("vat_excluded_total_cents")),
        "vat_eur": _eur_ou_rien(t.get("vat_total_cents")),
        "vat_rates": t.get("vat_rates"),
        "cancelled": t.get("cancelled"),
        "cancelled_at": iso(t.get("cancelled_at")),
        "cancelled_by": t.get("cancelled_by"),
        "appointment_ids": t.get("appointment_ids"),
    }


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def planity_list_pos_periods(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """List cash-register sessions (POS periods) opened in a date range.

        A period is one opening-to-closing of the register. Returns
        {period, count, periods: [{id, opened_at, closed_at, open, initial_eur,
        final_eur, receipts_count}]} — WITHOUT the tickets, which a period holds by
        the dozen. Use `planity_get_pos_period` for one period's tickets.

        Default: last 7 days. `open: true` means the session was never closed.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        brut = await c.list_pos_periods(salon_id, gte, lte, limit=limit)
        return {
            "period": periode(gte, lte),
            "count": len(brut),
            "periods": [
                {"id": p["id"], "opened_at": iso(p["created_at"]),
                 "closed_at": iso(p["closed_at"]), "open": p["open"],
                 "initial_eur": _eur_ou_rien(p["initial_amount_cents"]),
                 "final_eur": _eur_ou_rien(p["final_amount_cents"]),
                 "receipts_count": p["receipts_count"]}
                for p in brut
            ],
        }

    @mcp.tool()
    async def planity_get_pos_period(salon_id: str, period_id: str) -> dict:
        """One cash-register session with all its tickets.

        `period_id` comes from `planity_list_pos_periods`, or from a receipt link on
        an appointment (`planity_get_appointment` → `period_id`).

        ⚠️ Each ticket is an allow-list of fields, and the customer is an **id
        only** — no name, phone, email or address. Planity stores a full customer
        snapshot on every ticket; it does not come out of here. Resolve an id with
        `planity_get_customer` when you actually need the person.
        """
        c = await _client()
        p = await c.get_pos_period(salon_id, period_id)
        if p is None:
            return {"error": "not_found", "period_id": period_id}
        return {
            "id": p["id"], "opened_at": iso(p["created_at"]),
            "closed_at": iso(p["closed_at"]), "open": p["open"],
            "initial_eur": _eur_ou_rien(p["initial_amount_cents"]),
            "final_eur": _eur_ou_rien(p["final_amount_cents"]),
            "receipts_count": p["receipts_count"],
            "receipts": [_ticket_public(t) for t in p.get("receipts") or []],
        }

    @mcp.tool()
    async def planity_get_receipt(salon_id: str, period_id: str,
                                  receipt_id: str) -> dict:
        """One receipt (ticket) in full: lines, payments, VAT, seller, totals.

        A receipt lives UNDER its period — it has no address of its own, so
        `period_id` is required. Both ids come from `planity_list_pos_periods` /
        `planity_get_pos_period`, or from an appointment's `receipt_id` +
        `period_id`.

        ⚠️ Same allow-list: the customer is an **id only**. Planity keeps a full
        snapshot of her on the ticket — name, phone, email, address, the comment
        written about her — and none of it comes out of here. That is deliberate,
        and documented in this module's header.
        """
        c = await _client()
        t = await c.get_receipt(salon_id, period_id, receipt_id)
        if t is None:
            return {"error": "not_found", "receipt_id": receipt_id,
                    "period_id": period_id}
        return _ticket_public(t)

    @mcp.tool()
    async def planity_list_payment_methods(salon_id: str) -> list[dict]:
        """The salon's payment methods: {id, name, color, sort}.

        This is the table that gives `method` its name in receipts and in
        `planity_get_revenue_by_payment_method`.
        """
        c = await _client()
        return await c.list_payment_methods(salon_id)
