"""Le budget d'unités ne porte PAS d'argent (#1104) : aucune table, aucune colonne du
fragment `db/schema/credits.py::CREDITS` ne nomme une monnaie, une devise, un prix, une
facture ni un moyen de paiement. Le cœur compte des unités entières ; seul le commerce
les convertit.

Un nom suffit à faire échouer : une colonne `price_cents` ou `currency` posée « pour
plus tard » ferait du cœur un livre de compte, et rien d'autre ne le verrait. Lu sur le
DDL, sans base.
"""
from __future__ import annotations

import re

from oto_mcp.db._schema import _SCHEMA
from oto_mcp.db.schema.credits import CREDITS

# Racines interdites, cherchées dans chaque mot d'un identifiant (`snake_case` découpé).
_ARGENT = ("currency", "devise", "money", "monnaie", "price", "prix", "tarif", "cents",
           "centimes", "eur", "euro", "usd", "dollar", "microdollar", "invoice", "facture",
           "payment", "paiement", "card", "carte", "iban", "vat", "tva", "mollie", "stripe",
           "billing")


def _tables(ddl: str) -> dict[str, list[str]]:
    """{table: [colonnes]} des `CREATE TABLE` du fragment (un bloc par table)."""
    out = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", ddl, re.S):
        cols = [ligne.split()[0] for ligne in m.group(2).splitlines()
                if ligne.strip() and not ligne.strip().startswith(("--", "PRIMARY", "CHECK"))
                and ligne.startswith("    ") and not ligne.startswith("     ")]
        out[m.group(1)] = cols
    return out


def _mots(identifiant: str) -> set[str]:
    return set(identifiant.lower().split("_"))


def test_le_fragment_est_assemble():
    assert CREDITS in _SCHEMA, "le fragment des crédits n'est pas dans l'assemblage"


def test_le_budget_ne_porte_aucune_notion_d_argent():
    tables = _tables(CREDITS)
    assert set(tables) == {"credit_scales", "credit_scale_lines", "credit_payer_modes",
                           "credit_balances", "credit_holds", "credit_movements"}
    fautes = [f"{t}.{c}" for t, cols in tables.items() for c in [t, *cols]
              if _mots(c) & set(_ARGENT)]
    assert not fautes, (
        f"le budget d'unités nomme de l'argent : {fautes}. Le cœur compte des unités "
        "entières ; la conversion en monnaie est au commerce (#1104).")


def test_toute_quantite_est_un_entier():
    """Pas de `NUMERIC`, `REAL`, `MONEY` : une unité de budget ne se fractionne pas, et
    l'arrondi au plafond (`per_n`) se fait AVANT l'écriture."""
    assert not re.search(r"\b(NUMERIC|DECIMAL|REAL|DOUBLE|MONEY|FLOAT)\b", CREDITS, re.I)
