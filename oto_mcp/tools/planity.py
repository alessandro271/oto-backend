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

⚠️ **LISTE BLANCHE sur les données des clientes — exception assumée à « expose le
brut, l'agent décide ».** Le parti pris du connecteur est de rendre ce que l'amont
donne et de laisser l'agent composer. Elle s'arrête aux données personnelles d'un
TIERS : la cliente d'un salon n'est ni l'utilisatrice de l'outil ni sa cliente à
elle, elle n'a rien demandé, et son nom, son téléphone, son email, son adresse ou
le commentaire qu'on a écrit sur elle n'ont pas à traverser un transcript pour
répondre « combien de rendez-vous jeudi ».

Donc : tout outil qui touche un rendez-vous, un ticket, un avis ou une fiche rend
une liste blanche de champs NOMMÉS, jamais l'objet complet — et pour la cliente,
un **identifiant seulement**. Les outils qui servent une cliente nommément
(`planity_get_customer`, `planity_search_customers`) sont l'exception : c'est leur
objet, l'appelant les a demandés, et l'agent compose à partir de l'identifiant.

Ce n'est pas un oubli à corriger au nom du parti pris : c'est le parti pris, borné
là où elle coûterait à quelqu'un qui n'est pas dans la conversation. Le cœur
oto-core, lui, rend le brut — c'est une bibliothèque ; la frontière est ICI.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify
from . import planity_session
from .planity_session import _client, _eur, _eur_ou_rien, fenetre, iso


def _employe(e) -> dict:
    """Un enfant d'agenda tel qu'il sort — suppression et nature comprises."""
    return {"id": e.id, "name": e.name, "type": e.type, "title": e.title,
            "color": e.color, "calendar_id": e.calendar_id,
            "deleted": e.deleted, "deleted_at": iso(e.deleted_at)}


def _rdv_public(v: dict) -> dict:
    """Un rendez-vous réduit aux champs qui sortent — la LISTE BLANCHE.

    Elle est écrite ici et une seule fois, plutôt que dans chaque outil : ce qui
    protège une cliente ne doit pas dépendre de qui recopie quoi. Ce qui n'y est
    pas ne s'oublie pas, il est REFUSÉ — le nom, le téléphone, l'email de la
    cliente, le commentaire libre (il porte des noms), et l'objet brut.

    Le commentaire et le titre sont ajoutés PAR `planity_get_appointment`, qui est
    appelé pour un rendez-vous précis : c'est alors la note qu'on est venu
    chercher, pas un champ qui passe par là dans une liste de cent."""
    return {
        "id": v["id"],
        "employee_id": v["child_id"],
        "date": v.get("date"),
        "start": v.get("start"),
        "end": v.get("end"),
        "duration_minutes": v.get("duration_minutes"),
        "customer_id": v.get("customer_id"),
        "service_id": v.get("service_id"),
        "price_eur": _eur_ou_rien(v.get("price_cents")),
        "booked_via": v.get("booked_via"),
        "cancelled": v.get("cancelled"),
        "cancelled_at": iso(v.get("cancelled_at")),
        "receipt_id": (v.get("receipt") or {}).get("id"),
        "period_id": (v.get("receipt") or {}).get("period_id"),
        "created_at": iso(v.get("created_at")),
        "updated_at": iso(v.get("updated_at")),
    }


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
        """Full salon metadata: contact info, opening hours, team, calendars.

        `employees` lists the ACTIVE calendar children; `employees_deleted_count`
        says how many more exist and are gone. Use `planity_list_employees` with
        `include_deleted=true` to see them.
        """
        c = await _client()
        s = await c.get_salon(salon_id)
        return {
            "id": s.id, "name": s.name, "slug": s.slug, "phone": s.phone,
            "opening_hours": s.opening_hours, "db_shard": s.db_shard,
            "calendars": s.calendars,
            "employees": [_employe(e) for e in s.employees if not e.deleted],
            "employees_deleted_count": sum(1 for e in s.employees if e.deleted),
        }

    @mcp.tool()
    async def planity_list_employees(salon_id: str,
                                     include_deleted: bool = False) -> list[dict]:
        """Calendar children of a salon: {id, name, type, title, color, calendar_id,
        deleted, deleted_at}.

        Deleted children are EXCLUDED by default. Planity keeps them — their past
        appointments live in their calendar — so counting them as staff announces a
        team that has not existed for months. Pass `include_deleted=true` when you
        are reading history, not staffing.

        Not every child is a person: `type` and `title` are returned as Planity
        stores them, so a room, a chair or any bookable resource can be told apart
        from a colleague. They are passed through, not interpreted.
        """
        c = await _client()
        s = await c.get_salon(salon_id)
        return [_employe(e) for e in s.employees if include_deleted or not e.deleted]

    @mcp.tool()
    async def planity_list_services(salon_id: str,
                                    include_deleted: bool = False) -> list[dict]:
        """Bookable services catalog (flattened children).

        Planity groups services under categories; each leaf child is the actual
        bookable service. Returns {id, category_id, name, duration_minutes,
        bookable, description, price: {kind, ...}}.

        A service does NOT have one price. `price.kind` says which of four
        situations you are in — `fixed`, `range` (min/max), `on_quotation`, or
        `unpriced` (no price set at all, which is roughly half the catalogue).
        Euros AND raw cents are both returned: a range has no single euro figure to
        average, and rounding it would invent one.

        Deleted services are EXCLUDED by default, and so are the live services of a
        deleted CATEGORY — neither can be booked. Pass `include_deleted=true` to
        resolve an old `service_id` found on a past appointment or receipt.
        """
        c = await _client()
        coeur = planity_session._coeur()
        out = []
        for s in coeur.services.aplatir(await c.list_services(salon_id)):
            if s["deleted"] and not include_deleted:
                continue
            prix = s.pop("prices")
            s["deleted_at"] = iso(s["deleted_at"])
            s["category_deleted_at"] = iso(s["category_deleted_at"])
            out.append({
                **s,
                "price": {
                    "kind": prix["kind"],
                    "default_eur": _eur_ou_rien(prix["default_cents"]),
                    "min_eur": _eur_ou_rien(prix["min_cents"]),
                    "max_eur": _eur_ou_rien(prix["max_cents"]),
                    "default_cents": prix["default_cents"],
                    "min_cents": prix["min_cents"],
                    "max_cents": prix["max_cents"],
                },
            })
        return out

    @mcp.tool()
    async def planity_list_products(salon_id: str, in_stock_only: bool = False,
                                    include_deleted: bool = False) -> list[dict]:
        """Product catalog (shop items sold in the salon), with stock and reorder data.

        Returns {id, category_id, name, price_eur, ean, brand, stock_total,
        stock_lots, stock_threshold, stock_ceiling, supplier_id, deleted,
        deleted_at}.

        `stock_lots` is the point: stock is not a number but a list of purchase
        lots, each with its own `purchase_price_eur`. Flatten it and the margin
        disappears with it.

        `stock_threshold` / `stock_ceiling` / `supplier_id` are `null` when the
        salon does not use them — `null` is NOT `0`. A reorder rule that reads a
        missing threshold as zero orders everything, every time.
        """
        c = await _client()
        coeur = planity_session._coeur()
        out = []
        for p in coeur.stock.aplatir_produits(await c.list_products(salon_id)):
            if p["deleted"] and not include_deleted:
                continue
            if in_stock_only and p["stock_total"] <= 0:
                continue
            out.append({
                "id": p["id"], "category_id": p["category_id"], "name": p["name"],
                "price_eur": _eur(p["price_cents"]), "ean": p["ean"],
                "brand": p["brand"],
                "stock_total": p["stock_total"],
                "stock_lots": [
                    {"quantity": l["quantity"],
                     "purchase_price_eur": _eur_ou_rien(l["purchase_price_cents"]),
                     "created_at": iso(l["created_at"])}
                    for l in p["stock_lots"]
                ],
                "stock_threshold": p["stock_threshold"],
                "stock_ceiling": p["stock_ceiling"],
                "supplier_id": p["supplier_id"],
                "deleted": p["deleted"], "deleted_at": iso(p["deleted_at"]),
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
        """List appointments in a date range, optionally for one employee.

        Accepts presets: "today", "this_week", "7d", "30d"... Default: last 7 days.
        Reads every employee calendar of the salon unless `employee_id` narrows it.

        Times are the salon's WALL CLOCK, as Planity stores them — no UTC offset is
        added, because there is none to add and inventing one would be wrong half
        the year.

        A cancelled appointment is returned like any other, with `cancelled: true`
        — Planity has no status field, only a deletion date, and hiding them would
        hide cancellations from whoever is looking for them.

        ⚠️ Returns an allow-list of fields, and for the customer an **id only** —
        no name, no phone, no email, and not the free-text comment (it routinely
        contains people's names). Use `planity_get_appointment` for the comment of
        one appointment, and `planity_get_customer` to resolve an id. This is a
        deliberate exception to "expose the raw" — see the module docstring — not
        an omission to fix.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        rdv = await c.list_appointments(salon_id, iso(gte)[:10], iso(lte)[:10],
                                        employee_id=employee_id)
        return {
            "count": len(rdv), "from": iso(gte)[:10], "to": iso(lte)[:10],
            "truncated": len(rdv) > limit,
            "appointments": [_rdv_public(v) for v in rdv[:limit]],
        }

    @mcp.tool()
    async def planity_get_appointment(salon_id: str, vevent_id: str,
                                      employee_id: Optional[str] = None) -> dict:
        """One appointment in full, including its free-text comment.

        Without `employee_id`, every calendar of the salon is searched: an
        appointment id does not say which calendar it belongs to.

        ⚠️ Same allow-list as the listing, plus `comment` and `title` — this tool
        is asked for ONE appointment, so its note is what was asked for. The
        customer is still an **id only**; resolve it with `planity_get_customer`.
        """
        c = await _client()
        v = await c.get_appointment(salon_id, vevent_id, employee_id=employee_id)
        if v is None:
            return {"error": "not_found", "vevent_id": vevent_id}
        return {**_rdv_public(v),
                "comment": v.get("comment"), "title": v.get("title"),
                "sequence": v.get("sequence"),
                "service_origin_id": v.get("service_origin_id")}

    @mcp.tool()
    async def planity_list_recurring_appointments(
        salon_id: str, employee_id: Optional[str] = None, limit: int = 50,
    ) -> list[dict]:
        """Recurring appointments (standing bookings) of the salon.

        They live in a separate node and appear in NO date-range listing: a
        calendar that only holds recurrences reads as an empty calendar. Each one
        carries an `rrule` (RFC 5545) rather than a date.

        ⚠️ Same allow-list: customer id only, no name or contact details.
        """
        c = await _client()
        out = []
        for r in await c.list_recurring_appointments(salon_id, employee_id, limit):
            out.append({
                "id": r["id"], "employee_id": r["child_id"], "rrule": r["rrule"],
                "duration_minutes": r["duration_minutes"],
                "service_id": r["service_id"], "sequence": r["sequence"],
                "price_eur": _eur_ou_rien(r["price_cents"]),
                "customer_id": r["customer_id"],
                "created_at": iso(r["created_at"]),
                "updated_at": iso(r["updated_at"]),
                "all_day": r["all_day"],
            })
        return out
