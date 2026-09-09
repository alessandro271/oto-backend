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

from .planity_session import _bad, _client, _eur_ou_rien, fenetre, iso, periode

#: Le refus qu'on oppose à un balayage implicite du catalogue, et les deux chemins
#: qui coûtent un appel au lieu de six cents.
#:
#: ⚠️ **Il tombe AVANT toute lecture, et c'est le point.** Il a d'abord été écrit
#: après un `list_products` qui servait à compter les produits — donc un appel
#: condamné payait quand même une lecture, sur une session que l'appelant croyait
#: intacte. Compter pour refuser, c'est déjà avoir fait ce qu'on refuse.
_REFUS_BALAYAGE = (
    "Les mouvements de stock se lisent UN PRODUIT À LA FOIS — il n'existe pas "
    "d'index de temps au niveau du salon. Balayer un catalogue entier tiendrait la "
    "conversation des dizaines de secondes, donc cet outil ne le fait pas tout "
    "seul : passe `product_ids`. Les identifiants viennent de "
    "`planity_list_products` (un appel). Et pour une vue salon, "
    "`planity_get_revenue_breakdown` donne les quantités vendues par produit en UN "
    "appel — c'est de là qu'on part, puis on vient ici pour le détail des quelques "
    "produits qui sortent du lot.")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def planity_list_stock_movements(
        salon_id: str,
        product_ids: Optional[list[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        preset: Optional[str] = None,
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

        `product_ids` is REQUIRED in practice: movements are read one product at a
        time, so a whole-catalogue sweep is dozens of seconds and this tool will not
        do one implicitly. Without it, the call is refused immediately — **before
        any read** — naming the two cheaper paths: `planity_list_products` for the
        ids, and `planity_get_revenue_breakdown` for a salon-wide view in one call.
        """
        ids = [i for i in (product_ids or []) if i]
        if not ids:
            # AVANT `_client()` : un appel condamné n'ouvre même pas de session.
            raise _bad(_REFUS_BALAYAGE)
        c = await _client()
        gte, lte = fenetre(date_from, date_to, preset)
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
