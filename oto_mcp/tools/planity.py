"""Planity — référentiel, clientes et agenda d'un salon (LECTURE SEULE).

Le connecteur s'authentifie avec l'email et le mot de passe du compte Planity de
la personne, posés au coffre (`byo_user`, `secret_kind="basic_auth"`). Le client
vit dans oto-core (`oto.tools.planity`) ; ici il n'y a que des enveloppes minces :
résoudre le credential, appeler le client, rendre du JSON propre.

Les statistiques (chiffre d'affaires, collaboratrices, occupation, avis) sont dans
le module frère `planity_stats.py` — même connecteur, même namespace, même clé.

⚠️ **Ce connecteur s'authentifie avec l'email et le mot de passe du compte
Planity, et rien d'autre** — il n'emprunte aucun autre chemin d'authentification de
l'application Planity. Ce qu'il peut lire est donc exactement ce que ce compte peut
lire : le périmètre se règle en choisissant le compte, pas en configurant oto. La
fiche du connecteur (`connectors/docs/planity.md`) le dit à qui pose le credential.

Conventions de bord, héritées du serveur d'origine et inchangées (des agents et la
fiche connaissent ces noms et ces schémas) :
- les prix sont en centimes chez Planity, rendus en euros ;
- les horodatages sont en millisecondes, rendus en ISO (Europe/Paris) ;
- les outils temporels acceptent `date_from`/`date_to` (ISO ou preset), défaut 7 j.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify
from . import planity_session
from .planity_session import _client, _eur, fenetre, iso


async def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion ». Couvre `auth` SEUL.

    Joue la chaîne d'auth complète PUIS `list_salons` — et les deux comptent. La
    chaîne d'auth prouve que l'email et le mot de passe passent ; `list_salons`
    prouve que le jeton enrichi porte bien des salons ET que le WebSocket du
    Realtime Database répond. Un compte Planity dont le jeton n'ouvre aucun salon
    authentifie parfaitement et ne peut rien lire : rendre « connecté » là-dessus
    serait le vert creux que cette sonde existe pour empêcher.

    Sans effet de bord : trois POST d'authentification et des lectures. Planity
    n'expose ni compteur de crédits ni quota documenté — donc `auth`, jamais
    `auth+quota` : on n'a mesuré aucun solde.
    """
    # Les coordonnées de l'instance AVANT tout : sonder sans elles produirait un
    # 400 de Firebase, qu'on lirait comme « mauvais mot de passe » — et on ferait
    # reposer un credential parfaitement bon.
    coordonnees = await asyncio.to_thread(planity_session.endpoints)
    client = planity_session._coeur().PlanityClient(
        fields["email"], fields["password"], coordonnees)
    try:
        try:
            await client.auth.get_tokens()
        except Exception as e:
            statut = getattr(getattr(e, "response", None), "status_code", None)
            if statut in (400, 401, 403):
                raise connector_verify.NonAutorise(
                    "Planity a refusé cet email ou ce mot de passe.") from e
            raise
        salons = await client.list_salons()
        if not salons:
            raise connector_verify.NonAutorise(
                "Le compte s'authentifie mais n'ouvre aucun salon : son jeton ne "
                "porte aucun établissement. Vérifie qu'il s'agit bien d'un compte "
                "`pro.planity.com` rattaché à un salon.")
    finally:
        await client.close()


def register(mcp: FastMCP) -> None:
    # ⚠️ AUCUN import du cœur ici. Le connecteur reste MONTÉ même quand l'extra
    # `planity` d'oto-core manque ou que les coordonnées ne sont pas posées : il
    # est alors visible, sélectionnable, et chaque appel refuse en nommant ce qui
    # manque. Un connecteur qui disparaît du catalogue ne se remarque pas et ne
    # s'explique pas — c'est l'utilisatrice qui paie la différence.
    planity_session.avertir_au_demarrage()
    connector_verify.register("planity", _verify)

    # ═══════════════════════ Référentiel ═══════════════════════

    @mcp.tool()
    async def planity_list_salons() -> list[dict]:
        """List salons accessible to the user.

        Returns {id, name, slug, phone, opening_hours, employee_count, calendar_count}.
        Use `id` as `salon_id` in all other tools.
        """
        c = await _client()
        salons = await c.list_salons()
        return [
            {
                "id": s.id, "name": s.name, "slug": s.slug,
                "phone": s.phone, "opening_hours": s.opening_hours,
                "employee_count": len(s.employees),
                "calendar_count": len(s.calendars),
            }
            for s in salons
        ]

    @mcp.tool()
    async def planity_get_salon_info(salon_id: str) -> dict:
        """Full salon metadata: contact info, opening hours, team, calendars."""
        c = await _client()
        s = await c.get_salon(salon_id)
        return {
            "id": s.id, "name": s.name, "slug": s.slug, "phone": s.phone,
            "opening_hours": s.opening_hours, "db_shard": s.db_shard,
            "calendars": s.calendars,
            "employees": [
                {"id": e.id, "name": e.name, "color": e.color, "calendar_id": e.calendar_id}
                for e in s.employees
            ],
        }

    @mcp.tool()
    async def planity_list_employees(salon_id: str) -> list[dict]:
        """List employees (collaborateurs) of a salon: {id, name, color, calendar_id}."""
        c = await _client()
        s = await c.get_salon(salon_id)
        return [
            {"id": e.id, "name": e.name, "color": e.color, "calendar_id": e.calendar_id}
            for e in s.employees
        ]

    @mcp.tool()
    async def planity_list_services(salon_id: str) -> list[dict]:
        """Bookable services catalog (flattened children).

        Planity groups services under categories; each leaf child is the actual
        bookable service. Returns: {id, category_id, name, price_eur, duration_minutes, bookable}.
        """
        c = await _client()
        raw = await c.list_services(salon_id)
        out = []
        for cat_id, cat in raw.items():
            if not isinstance(cat, dict):
                continue
            children = cat.get("children") or {}
            if not isinstance(children, dict):
                continue
            for child_id, s in children.items():
                if not isinstance(s, dict):
                    continue
                price = s.get("price")
                if isinstance(price, dict):
                    price = price.get("default")
                out.append({
                    "id": child_id,
                    "category_id": cat_id,
                    "name": (s.get("name") or "").strip(),
                    "price_eur": _eur(price),
                    "duration_minutes": s.get("duration"),
                    "bookable": s.get("bookable", True),
                    "description": (s.get("description") or "")[:300],
                })
        return out

    @mcp.tool()
    async def planity_list_products(salon_id: str, in_stock_only: bool = False) -> list[dict]:
        """Product catalog (shop items sold in the salon).

        Returns leaf products flattened from categories:
        {id, category_id, name, price_eur, ean, stock_total, deleted}.
        """
        c = await _client()
        raw = await c.list_products(salon_id)
        out = []
        for cat_id, cat in raw.items():
            if not isinstance(cat, dict):
                continue
            children = cat.get("children") or {}
            if not isinstance(children, dict):
                continue
            for pid, p in children.items():
                if not isinstance(p, dict):
                    continue
                stocks = p.get("stocks") or {}
                stock_total = 0
                if isinstance(stocks, dict):
                    for s in stocks.values():
                        if isinstance(s, dict):
                            stock_total += int(s.get("quantity", 0) or 0)
                deleted = bool(p.get("deletedAt"))
                if in_stock_only and (stock_total <= 0 or deleted):
                    continue
                out.append({
                    "id": pid,
                    "category_id": cat_id,
                    "name": (p.get("name") or "").strip(),
                    "price_eur": _eur(p.get("price")),
                    "ean": p.get("eanCode"),
                    "stock_total": stock_total,
                    "deleted": deleted,
                })
        return out

    # ═══════════════════════ Clientes ═══════════════════════

    @mcp.tool()
    async def planity_search_customers(salon_id: str, query: str = "",
                                       limit: int = 10) -> list[dict]:
        """Search customers by name/phone/email within a salon (Algolia).

        Empty query returns most recent customers.
        """
        c = await _client()
        hits = await c.search_customers(salon_id, query=query, limit=limit)
        return [
            {
                "id": h.get("id") or h.get("objectID"),
                "name": (h.get("name") or "").strip(),
                "phone": h.get("phone"),
                "email": h.get("email"),
                "gender": h.get("gender"),
                "created_at": iso(h.get("createdAt")),
            }
            for h in hits
        ]

    @mcp.tool()
    async def planity_get_customer(salon_id: str, customer_id: str) -> dict:
        """Full customer profile (name, phone, address, note, vevent IDs).

        Does NOT include stats/receipts — use planity_get_customer_stats /
        planity_get_customer_receipts for those.
        """
        c = await _client()
        p = await c.get_customer(salon_id, customer_id)
        vevents = p.get("vevents") or {}
        return {
            "id": customer_id,
            "name": (p.get("name") or "").strip(),
            "phone": p.get("phone"),
            "email": p.get("email"),
            "address": p.get("address"),
            "postal_code": p.get("postalCode"),
            "city": p.get("city"),
            "gender": p.get("gender"),
            "comment": p.get("comment"),
            "created_at": iso(p.get("createdAt")),
            "skip_marketing_sms": p.get("skipMarketingSMS"),
            "vevent_ids": list(vevents.keys()) if isinstance(vevents, dict) else [],
        }

    @mcp.tool()
    async def planity_get_customer_stats(salon_id: str, customer_id: str) -> dict:
        """Appointments count + revenue stats (service/product breakdown) for a customer.

        Average basket, revenue in euros, appointment frequency in days.
        """
        c = await _client()
        raw = await c.get_customer_stats(salon_id, customer_id)
        rev = raw.get("revenue") or {}
        appts = raw.get("appointments") or {}
        return {
            "appointments": {
                "total": appts.get("total", 0),
                "by_web": appts.get("byWeb", 0),
                "by_pro": appts.get("byPro", 0),
                "frequency_days": appts.get("frequency", 0),
            },
            "revenue": {
                "total_eur": _eur(rev.get("total")),
                "average_basket_eur": _eur(rev.get("average")),
                "by_service_eur": _eur(rev.get("totalByService")),
                "by_product_eur": _eur(rev.get("totalByProduct")),
                "service_share_pct": round(rev.get("rateByService", 0), 1),
                "product_share_pct": round(rev.get("rateByProduct", 0), 1),
            },
        }

    @mcp.tool()
    async def planity_get_customer_receipts(salon_id: str, customer_id: str,
                                            limit: int = 10) -> list[dict]:
        """Receipt history (tickets) for a customer, most recent first.

        Each ticket has total_eur + lines (services/products mix).
        """
        c = await _client()
        raw = await c.get_customer_receipts(salon_id, customer_id)
        # Sort by createdAt desc
        raw_sorted = sorted(raw, key=lambda r: r.get("createdAt", 0), reverse=True)
        out = []
        for r in raw_sorted[:limit]:
            lines = r.get("lines") or []
            out.append({
                "id": r.get("receiptId"),
                "date": iso(r.get("createdAt")),
                "total_eur": _eur(sum(l.get("price", 0) for l in lines)),
                "lines": [
                    {
                        "price_eur": _eur(l.get("price")),
                        "service_id": l.get("serviceId"),
                        "product_id": l.get("productId"),
                        "cure_id": l.get("cureId"),
                        "gift_voucher_id": l.get("giftVoucherId"),
                    }
                    for l in lines
                ],
            })
        return out

    # ═══════════════════════ Agenda ═══════════════════════

    @mcp.tool()
    async def planity_list_appointments(
        salon_id: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
        employee_id: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """List appointments (vevents) in a date range, optionally filtered by employee.

        Accepts presets: "today", "this_week", "7d", "30d", etc.
        Default: last 7 days.
        Returns {count, vevents: [{id, start, end, customer_id, seller_id, services, status}]}.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        raw = await c.list_appointments(salon_id)
        out = []
        for vid, v in (raw or {}).items():
            if not isinstance(v, dict):
                continue
            start_ms = v.get("start")
            if not isinstance(start_ms, (int, float)):
                continue
            if start_ms < gte or start_ms > lte:
                continue
            seller = v.get("seller_id") or v.get("sellerId") or v.get("child")
            if employee_id and seller != employee_id:
                continue
            out.append({
                "id": vid,
                "start": iso(start_ms),
                "end": iso(v.get("end")),
                "customer_id": v.get("customer_id") or v.get("customerId"),
                "seller_id": seller,
                "services": v.get("services") or v.get("serviceIds"),
                "status": v.get("status"),
                "title": v.get("title") or v.get("name"),
            })
        out.sort(key=lambda x: x.get("start") or "")
        return {"count": len(out), "from": iso(gte), "to": iso(lte),
                "vevents": out[:limit]}

    @mcp.tool()
    async def planity_get_appointment(salon_id: str, vevent_id: str) -> dict:
        """Full appointment detail for a single vevent."""
        c = await _client()
        v = await c.get_appointment(salon_id, vevent_id)
        if not v:
            return {"error": "not_found"}
        return {
            "id": vevent_id,
            "start": iso(v.get("start")),
            "end": iso(v.get("end")),
            "customer_id": v.get("customer_id") or v.get("customerId"),
            "seller_id": v.get("seller_id") or v.get("sellerId") or v.get("child"),
            "services": v.get("services") or v.get("serviceIds"),
            "status": v.get("status"),
            "title": v.get("title") or v.get("name"),
            "notes": v.get("note") or v.get("comment"),
            "raw": v,
        }
