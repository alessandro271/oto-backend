"""Planity — le STOCK d'un salon : mouvements, fournisseurs, commandes, sorties.

Module frère de `planity.py` (le catalogue produits et ses lots vivent là-bas, sur
`planity_list_products`) : même connecteur, même namespace, même session. Ici, ce
qui BOUGE — chaque entrée et chaque sortie, avec sa date, son type et son prix
d'achat.

Il n'y a **pas d'outil de prévision de commande**, et c'est délibéré : la règle
(combien de jours de couverture, quel délai fournisseur, quelles familles on
réassort) appartient au salon, pas au connecteur. Une prévision codée ici serait la
nôtre, figée, et fausse chez le suivant. Les outils rendent les faits ; l'agent
compose — la recette tient en trois appels, elle est dans la fiche du connecteur.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from . import planity_session
from .planity_session import _bad, _client, _eur_ou_rien, fenetre, iso, periode

#: Au-delà, un balayage du catalogue entier n'est plus une lecture, c'est une
#: attente : les mouvements se lisent PAR PRODUIT (une lecture bornée chacun), et
#: un salon à neuf cents références y passerait une demi-minute. On refuse en
#: nommant les deux chemins moins chers, plutôt que de tronquer en silence — une
#: liste tronquée de ventes se lit comme un produit qui ne se vend plus.
_PRODUITS_MAX = 150


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def planity_list_stock_movements(
        salon_id: str,
        product_ids: Optional[list[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
        max_products: int = _PRODUITS_MAX,
    ) -> dict:
        """Raw stock movements in a date range: every in and out, per product.

        Returns {period, products_scanned, count, movements: [{product_id, id,
        date, type, quantity, purchase_price_eur, motive}]}, oldest first.

        `type` is Planity's own: `creation` (stock created), `sale`, `update`
        (manual correction), `saleCancellation` (a sale given back). Types outside
        that list are returned as-is rather than dropped.

        `purchase_price_eur` is null when the movement carries no amount — Planity
        also stores the string `"any"` there, for a movement that targets no
        particular purchase lot. `purchase_price_raw` keeps what was actually
        written. Null means "not an amount", never "free".

        Movements are read ONE PRODUCT AT A TIME — there is no whole-catalogue
        index. Pass `product_ids` whenever you know which products you care about.
        Without it the whole live catalogue is swept, which is refused above
        `max_products` rather than made to crawl: for a salon-wide view, the cheap
        path is `planity_get_revenue_breakdown` (quantities sold per product, one
        call) and `planity_list_products` (stock and lots, one call) — this tool is
        for the detail on the handful of products that then look interesting.
        """
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
        ids = list(product_ids or [])
        if not ids:
            coeur = planity_session._coeur()
            vivants = [p for p in coeur.stock.aplatir_produits(
                await c.list_products(salon_id)) if not p["deleted"]]
            if len(vivants) > max_products:
                raise _bad(
                    f"Ce salon a {len(vivants)} produits actifs et les mouvements se "
                    f"lisent un produit à la fois : balayer tout le catalogue tiendrait "
                    f"la conversation des dizaines de secondes. Passe `product_ids` "
                    f"(les ids viennent de `planity_list_products`), ou "
                    f"`max_products={len(vivants)}` si tu veux vraiment tout — et pour "
                    f"une vue salon, `planity_get_revenue_breakdown` donne les "
                    f"quantités vendues par produit en UN appel.")
            ids = [p["id"] for p in vivants]
        mouvements = await c.list_stock_movements(salon_id, ids, gte, lte)
        return {
            "period": periode(gte, lte),
            "products_scanned": len(ids),
            "count": len(mouvements),
            "movements": [
                {"product_id": m["product_id"], "id": m["id"],
                 "date": iso(m["created_at"]), "type": m["type"],
                 "quantity": m["quantity"],
                 "purchase_price_eur": _eur_ou_rien(m["purchase_price_cents"]),
                 "purchase_price_raw": m["purchase_price_raw"],
                 "motive": m["motive"]}
                for m in mouvements
            ],
        }

    @mcp.tool()
    async def planity_list_suppliers(salon_id: str) -> dict:
        """Suppliers declared for the salon's shop.

        Returns {count, suppliers}. An empty list is a normal answer: a salon that
        orders by phone or email declares none, and that is not an error to retry.
        """
        fournisseurs = await (await _client()).list_suppliers(salon_id)
        return {"count": len(fournisseurs), "suppliers": fournisseurs}

    @mcp.tool()
    async def planity_list_product_orders(salon_id: str,
                                          cursor: Optional[str] = None) -> dict:
        """One page of product (restocking) orders. Returns {count, orders, cursor}.

        Pass the returned `cursor` back to get the next page; a `cursor` of null
        means there is no next page. An empty list is a normal answer for a salon
        that does not order through Planity.
        """
        page = await (await _client()).list_product_orders(salon_id, cursor=cursor)
        return {"count": len(page["data"]), "orders": page["data"],
                "cursor": page["cursor"]}

    @mcp.tool()
    async def planity_list_mass_stock_removals(salon_id: str,
                                               limit: int = 20) -> dict:
        """Bulk stock write-offs (inventory correction, breakage, expiry), newest last.

        Returns {count, removals: [{id, date, products_count, products}]}. These do
        NOT appear as `sale` movements: a stock that dropped without a sale is
        usually one of these, and looking only at sales makes the difference
        unexplainable.
        """
        sorties = await (await _client()).list_mass_stock_removals(salon_id, limit)
        return {
            "count": len(sorties),
            "removals": [{"id": s["id"], "date": iso(s["created_at"]),
                          "products_count": s["products_count"],
                          "products": s["products"]}
                         for s in sorties],
        }
