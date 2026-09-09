"""Planity — les CHIFFRES d'un salon : caisse, collaboratrices, occupation, avis.

Module frère de `planity.py` (référentiel, clientes, agenda) : même connecteur,
même namespace `planity_*`, même credential, même session (`planity_session`).
Séparés parce que les vingt outils du connecteur ne tiennent pas dans un fichier,
et que la ligne de partage qui a du sens suit les deux familles de sources — les
chiffres d'un côté, le référentiel et l'agenda de l'autre.

⚠️ **`planity_get_revenue_breakdown` peut rendre un `by_seller` VIDE**, et ce
n'est PAS un salon sans ventes : Planity ne renseigne pas cette ventilation-là.
La répartition par collaboratrice qui fait foi est `planity_get_seller_stats` —
c'est elle qu'il faut lire, et c'est ce qu'il faut répondre à qui s'étonne.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .planity_session import _client, _eur, fenetre, iso


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def planity_get_revenue_summary(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Revenue KPIs for a period: total CA (TTC/HT), ticket count, VAT, average basket.

        Default: last 7 days. Use preset for quick ranges ("today", "week", "month", "30d"...).
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        ki = await c.get_key_indicators(salon_id, gte, lte)
        return {
            "from": iso(gte), "to": iso(lte),
            "revenue_ttc_eur": _eur(ki.get("revenueWithVAT")),
            "revenue_ht_eur": _eur(ki.get("revenueWithoutVAT")),
            "ticket_count": ki.get("amountOfReceipts", 0),
            "vat_eur": _eur(ki.get("VATValue")),
            "average_basket_eur": _eur(ki.get("averageBasket")),
        }

    @mcp.tool()
    async def planity_get_daily_revenue(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> list[dict]:
        """Daily revenue breakdown: [{date, revenue_ttc_eur, revenue_ht_eur, quantity}].

        Useful for trend analysis or plotting.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        data = await c.get_revenues(salon_id, gte, lte)
        out = []
        for ts_str, bucket in sorted((data.get("all") or {}).items()):
            out.append({
                "date": iso(int(ts_str)),
                "revenue_ttc_eur": _eur(bucket.get("revenueWithVAT")),
                "revenue_ht_eur": _eur(bucket.get("revenueWithoutVAT")),
                "quantity": bucket.get("quantity", 0),
            })
        return out

    @mcp.tool()
    async def planity_get_best_customers(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Top-spending customers for a period (Planity's 'best customers' analysis)."""
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        return await c.get_best_customers(salon_id, gte, lte)

    @mcp.tool()
    async def planity_get_new_customers(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Customers acquired for the first time during the period."""
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        return await c.get_new_customers(salon_id, gte, lte)

    @mcp.tool()
    async def planity_get_customer_frequencies(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Overall customer visit frequency distribution (who comes how often)."""
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        return await c.get_overall_frequencies(salon_id, gte, lte)

    @mcp.tool()
    async def planity_get_revenue_breakdown(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Multi-dimensional revenue breakdown: total CA + splits by service, product,
        seller (employee), other line types, and gift vouchers, in a single call.

        All amounts in euros. Use this to answer "comment se décompose mon CA ?".
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        raw = await c.get_revenue_breakdown(salon_id, gte, lte)
        totals = raw.get("totals") or {}

        def _block(key: str) -> dict:
            b = raw.get(key) or {}
            t = b.get("totals") or {}
            data = b.get("data") or {}
            items = []
            if isinstance(data, dict):
                for cat_key, cat in data.items():
                    if not isinstance(cat, dict):
                        continue
                    for child in cat.get("children") or []:
                        if not isinstance(child, dict):
                            continue
                        items.append({
                            "name": child.get("name"),
                            "revenue_eur": _eur(child.get("revenue")),
                            "quantity": child.get("amount"),
                            "discount_eur": _eur(child.get("discount")),
                            "margin_eur": _eur(child.get("margin")),
                            "category_id": cat_key,
                        })
            items.sort(key=lambda i: i["revenue_eur"], reverse=True)
            return {
                "total_eur": _eur(t.get("totalRevenue")),
                "quantity": t.get("totalAmount", 0),
                "items": items,
            }

        # bySeller may be list or dict
        by_seller_raw = raw.get("bySeller") or []
        sellers_out = []
        if isinstance(by_seller_raw, list):
            for row in by_seller_raw:
                if isinstance(row, dict):
                    sellers_out.append({
                        "seller_id": row.get("sellerId"),
                        "revenue_eur": _eur(row.get("revenue")),
                        "quantity": row.get("amount"),
                    })

        return {
            "from": iso(gte), "to": iso(lte),
            "grand_total_eur": _eur(totals.get("totalRevenue")),
            "grand_total_ht_eur": _eur(totals.get("totalPriceVATExcluded")),
            "total_discount_eur": _eur(totals.get("totalDiscount")),
            "ticket_count": totals.get("totalAmount", 0),
            "profitability_rate": totals.get("profitabilityRate"),
            "by_service": _block("byService"),
            "by_product": _block("byProduct"),
            "by_other": _block("byOther"),
            "by_gift_voucher": _block("byGiftVoucherAtSale"),
            "by_seller": sellers_out,
        }

    @mcp.tool()
    async def planity_get_seller_stats(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> list[dict]:
        """Per-employee (seller) stats for a period.

        Returns revenue, average basket, and appointment count for each employee.
        Use this to compare one employee against the rest of the team.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        raw = await c.get_calendar_stats(salon_id, gte, lte)
        salon = await c.get_salon(salon_id)
        name_by_id = {e.id: e.name for e in salon.employees}

        # The response has two shapes:
        #   - data: list of tuples [sellerId, ?, ?, ?, totalRevenue_cents, avgBasket, ?, ?]
        #   - bySeller.data: dict keyed by sellerId, with
        #     {onlineAppointments, offlineAppointments, ...}
        tuples = raw.get("data") or []
        by_seller = (raw.get("bySeller") or {}).get("data") or {}
        out = []
        for row in tuples:
            if not isinstance(row, list) or len(row) < 6:
                continue
            sid = row[0]
            out.append({
                "seller_id": sid,
                "name": name_by_id.get(sid, "?"),
                "revenue_eur": _eur(row[4]),
                "average_basket_eur": (round(float(row[5]), 2)
                                       if isinstance(row[5], (int, float)) else None),
                "appointments_done": row[1],
                "services_count": row[2],
                "appointments": (by_seller.get(sid) or {}),
            })
        out.sort(key=lambda s: s["revenue_eur"], reverse=True)
        return out

    @mcp.tool()
    async def planity_get_occupancy_rate(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Occupancy heatmap: matrix of (day of week × time slot) with rates 0..1+.

        1.0 = fully booked. >1.0 = double-booked. Useful for spotting the busiest
        hours and idle slots.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        raw = await c.get_occupancy_rate(salon_id, gte, lte)
        return {
            "from": iso(gte), "to": iso(lte),
            "matrix": raw.get("matrix"),
            "raw": raw,
        }

    @mcp.tool()
    async def planity_get_reviews_stats(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Reviews ratings aggregated by calendar and by service (Planity reviews).

        Useful for "quelles prestations sont le mieux notées".
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        return await c.get_reviews_stats(salon_id, gte, lte)
