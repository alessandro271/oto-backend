"""Store des factures et avoirs d'abonnement (#488) — la TRACE, pas le document.

Le document vit chez **Pennylane** : c'est lui qui porte la numérotation continue,
le PDF et la valeur probante. Cette table dit, pour chaque paiement encaissé, ce
qui a été émis en face — ou ce qui ne l'est pas, et pourquoi.

⚠️ **Depuis le 2026-09-09, plus rien n'émet automatiquement** : une ligne neuve
naît puis passe en `held` (`billing_invoices/emission.py` dit la décision et ce
qu'elle attend). Les trois statuts sont donc `pending` (une tentative a échoué —
lignes d'AVANT la coupure), `issued` (document réel, d'avant la coupure ou posé à
la main), `held` (tracé, à poser). Le store, lui, garde son vocabulaire ENTIER —
`mark_billing_invoice_issued`, le PDF — parce que la reprise en main en aura
besoin, quelle que soit la forme retenue.

Trois propriétés portent tout le reste :

1. **La ligne naît AVANT toute autre chose**, à l'encaissement. Un paiement sans
   facture reste alors visible et reprenable ; sans elle, rien ne distinguerait un
   encaissement non facturé d'un encaissement oublié — un paiement muet.
2. **L'idempotence est en base** : `UNIQUE (payment_row_id, kind)`. Un webhook
   rejoué retombe sur la ligne existante et ne crée ni seconde facture ni second
   avoir. La garde est la contrainte, pas une lecture préalable (deux webhooks
   simultanés passeraient toute lecture).
3. **Le PDF ne sort jamais d'une liste.** `pdf` est un `BYTEA` et aucune lecture
   d'ici ne fait `SELECT *` : le row factory ne normalise que les dates, et des
   octets remontés dans un dict servi en JSON feraient une 500 à la sérialisation
   — sur le chemin le moins emprunté de la surface. Le PDF a son getter dédié.
"""
from __future__ import annotations

from typing import Any, Optional

from ._conn import _connect

# Ce qu'une lecture rend — TOUT sauf `pdf`. La colonne d'octets est nommée par son
# absence ici, et `has_pdf` dit ce qu'un client a besoin de savoir : y a-t-il un
# document à télécharger.
_INVOICE_COLS = (
    "id, org_id, payment_row_id, payment_ref, kind, status, external_reference, "
    "pennylane_customer_id, pennylane_invoice_id, credited_invoice_id, number, "
    "currency, amount_ht, vat_rate_bps, vat_amount, amount_ttc, vat_scheme, "
    "period_start, period_end, issued_at, pdf_filename, pdf_url, emailed_at, "
    "email_to, attempts, error_code, error_detail, last_attempt_at, created_at, "
    "updated_at, (pdf IS NOT NULL) AS has_pdf"
)

INVOICE_KINDS = ("invoice", "credit_note")


def ensure_billing_invoice(org_id: int, payment_row_id: int, *, kind: str = "invoice",
                           payment_ref: Optional[str] = None,
                           amount_ttc: Optional[int] = None) -> dict:
    """La ligne de trace d'un encaissement — créée si elle n'existe pas, rendue sinon.

    C'est le PREMIER geste, avant tout le reste : ce qui suit peut échouer ou
    attendre une main, la trace, elle, est déjà écrite. `ON CONFLICT DO NOTHING` plutôt
    qu'un `SELECT` préalable — deux webhooks concurrents sur le même paiement
    passeraient toute lecture, seule la contrainte les départage.

    `amount_ttc` sert l'AVOIR : le montant remboursé n'est connu que du webhook qui
    l'a vu passer, et une reprise horaire n'a plus aucun moyen de le retrouver (le
    webhook Mollie ne porte que l'id du paiement). Écrit à la CRÉATION, il est
    l'intention ; une émission le réécrira avec ce que le fournisseur a émis.
    """
    if kind not in INVOICE_KINDS:
        raise ValueError(f"kind de facture inconnu : {kind!r} "
                         f"({' | '.join(INVOICE_KINDS)})")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO billing_invoices "
            "  (org_id, payment_row_id, kind, payment_ref, amount_ttc) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (payment_row_id, kind) DO NOTHING",
            (org_id, payment_row_id, kind, payment_ref, amount_ttc))
        return conn.execute(
            f"SELECT {_INVOICE_COLS} FROM billing_invoices "
            "WHERE payment_row_id = %s AND kind = %s",
            (payment_row_id, kind)).fetchone()


def get_billing_invoice(invoice_id: int) -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_INVOICE_COLS} FROM billing_invoices WHERE id = %s",
            (invoice_id,)).fetchone()


def get_billing_invoice_for_payment(payment_row_id: int,
                                    kind: str = "invoice") -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            f"SELECT {_INVOICE_COLS} FROM billing_invoices "
            "WHERE payment_row_id = %s AND kind = %s", (payment_row_id, kind)).fetchone()


def list_billing_invoices(org_id: int, limit: int = 24) -> list[dict]:
    """Les documents d'une org, plus récents d'abord — factures ET avoirs."""
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT {_INVOICE_COLS} FROM billing_invoices WHERE org_id = %s "
            "ORDER BY created_at DESC LIMIT %s", (org_id, limit)))


def mark_billing_invoice_issued(
    invoice_id: int, *,
    pennylane_invoice_id: Optional[int],
    number: Optional[str],
    external_reference: Optional[str] = None,
    pennylane_customer_id: Optional[int] = None,
    credited_invoice_id: Optional[int] = None,
    currency: str = "eur",
    amount_ht: Optional[int] = None,
    vat_rate_bps: Optional[int] = None,
    vat_amount: Optional[int] = None,
    amount_ttc: Optional[int] = None,
    vat_scheme: Optional[str] = None,
    period_start: Any = None,
    period_end: Any = None,
    issued_at: Any = None,
) -> None:
    """Le document est émis et FINALISÉ chez Pennylane : on grave ce qu'il porte.

    `error_code`/`error_detail` sont remis à NULL — une émission réussie efface la
    cause d'un échec précédent, sinon la ligne dirait à la fois « émise » et
    « voici pourquoi elle ne l'est pas »."""
    with _connect() as conn:
        conn.execute(
            "UPDATE billing_invoices SET status='issued', "
            "pennylane_invoice_id=%s, number=%s, "
            "external_reference=COALESCE(%s, external_reference), "
            "pennylane_customer_id=COALESCE(%s, pennylane_customer_id), "
            "credited_invoice_id=COALESCE(%s, credited_invoice_id), "
            "currency=%s, amount_ht=%s, vat_rate_bps=%s, vat_amount=%s, "
            "amount_ttc=%s, vat_scheme=%s, period_start=%s, period_end=%s, "
            "issued_at=%s, error_code=NULL, error_detail=NULL, updated_at=NOW() "
            "WHERE id=%s",
            (pennylane_invoice_id, number, external_reference, pennylane_customer_id,
             credited_invoice_id, currency, amount_ht, vat_rate_bps, vat_amount,
             amount_ttc, vat_scheme, period_start, period_end, issued_at, invoice_id))


def hold_billing_invoice(invoice_id: int, code: str, detail: str = "") -> None:
    """Met la ligne EN ATTENTE D'UNE MAIN (`status='held'`) et dit pourquoi.

    `held` est le troisième et dernier statut, ajouté le 2026-09-09 avec l'arrêt de
    l'émission automatique (`billing_invoices/emission.py` dit pourquoi). Il se lit
    « l'encaissement est tracé, aucun document n'est dû tout seul, quelqu'un doit
    le poser ». Ce n'est ni un échec ni une file d'attente :

    - **le balayage l'ignore SANS le savoir** : `pending_billing_invoices` filtre
      `status = 'pending'`, une ligne tenue en sort d'elle-même. Rien à ajouter
      côté reprise, donc rien à retirer le jour où l'émission reviendra ;
    - **`attempts` n'est PAS incrémenté**, et c'est le point : il compte les appels
      réellement passés au fournisseur. L'incrémenter ferait lire un fournisseur en
      panne là où il y a une décision. La fonction qui le faisait
      (`mark_billing_invoice_failed`) est partie avec les appels qu'elle comptait.

    ⚠️ Aucun DDL : `status` est un TEXT libre, et prod/preprod partagent la même
    base. La contrainte est dans la tête des lecteurs, pas dans le schéma.

    Deux gardes dans le `WHERE` : **jamais un document déjà émis** (`pending` seul
    est retenu — un `issued` d'avant la coupure ne se dé-facture pas), et une
    ligne déjà tenue n'est pas retouchée à chaque tick, sinon `updated_at`
    avancerait d'une heure en une heure sans qu'il se soit rien passé."""
    with _connect() as conn:
        conn.execute(
            "UPDATE billing_invoices SET status='held', error_code=%s, "
            "error_detail=%s, updated_at=NOW() WHERE id=%s AND status='pending'",
            (code, (detail or "")[:500], invoice_id))


def set_billing_invoice_pdf(invoice_id: int, pdf: Optional[bytes], *,
                            filename: Optional[str] = None,
                            url: Optional[str] = None) -> None:
    """Range le document téléchargé. L'URL Pennylane EXPIRE (30 min) : elle est
    conservée comme trace de provenance, jamais servie comme lien."""
    with _connect() as conn:
        conn.execute(
            "UPDATE billing_invoices SET pdf=%s, pdf_filename=%s, "
            "pdf_url=COALESCE(%s, pdf_url), updated_at=NOW() WHERE id=%s",
            (pdf, filename, url, invoice_id))


def get_billing_invoice_pdf(invoice_id: int) -> Optional[dict]:
    """Le SEUL chemin qui lit les octets — org comprise, pour que l'appelant puisse
    vérifier l'appartenance sans une seconde requête."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, org_id, kind, number, pdf, pdf_filename "
            "FROM billing_invoices WHERE id = %s", (invoice_id,)).fetchone()
    if row and isinstance(row.get("pdf"), memoryview):
        row["pdf"] = bytes(row["pdf"])
    return row


# ⚠️ `emailed_at` / `email_to` n'ont PLUS d'écrivain depuis le 2026-09-09 : la
# plateforme n'envoie plus de facture par e-mail (`billing_invoices/emission.py` dit
# pourquoi). Les deux colonnes restent — la base est PARTAGÉE prod/preprod, aucun DDL
# ne se joue ici — et datent les envois d'AVANT cette date ; elles sont `NULL` pour
# tout document postérieur, et se lisent comme une archive, jamais comme un état.


def pending_billing_invoices(limit: int = 50) -> list[dict]:
    """La file du `billing_runner` — les lignes qu'un tick doit revisiter.

    ⚠️ **`held` en est exclu par le filtre `status = 'pending'`**, et c'est ce qui
    rend l'arrêt de l'émission (2026-09-09) silencieux : une ligne tenue n'est pas
    en attente d'une reprise, elle attend une main. Rien n'a été ajouté ici pour
    l'écarter — donc rien ne sera à retirer le jour où l'émission reviendra.

    Plus ANCIENNES d'abord : une facture qui n'est pas partie depuis trois jours
    passe avant celle d'il y a une heure, dont l'échec est peut-être transitoire."""
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT {_INVOICE_COLS} FROM billing_invoices WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT %s", (limit,)))


def paid_payments_without_invoice(limit: int = 50, *, since=None) -> list[dict]:
    """Encaissements qui n'ont même pas de ligne de trace (`billing_invoices`).

    C'est le filet du filet : si un appel inline n'a jamais eu lieu — process tué
    entre l'écriture de `paid` et l'émission, chemin de code futur qui grave un
    encaissement sans le dire ici — la facture se rattrape quand même. Sans lui,
    la garantie « jamais un paiement sans trace de facture » ne tiendrait qu'à la
    discipline des appelants.

    ⚠️ **`amount_ht IS NULL` est EXCLU, et c'est la règle (c) de #488.** Ce sont les
    deux encaissements du 25/08/2026, débités du HT sans TVA avant que la règle
    n'existe : sans décomposition fiscale, aucune facture conforme n'est
    calculable — en fabriquer une reviendrait à inventer une TVA qui n'a pas été
    collectée. Ils se régularisent à la main, et `docs/billing.md` dit comment."""
    clause = "AND p.created_at > %s " if since is not None else ""
    args: list = [] if since is None else [since]
    with _connect() as conn:
        return list(conn.execute(
            "SELECT p.* FROM billing_payments p "
            "WHERE p.status = 'paid' AND p.amount_ht IS NOT NULL " + clause +
            "  AND NOT EXISTS (SELECT 1 FROM billing_invoices i "
            "                  WHERE i.payment_row_id = p.id AND i.kind = 'invoice') "
            "ORDER BY p.created_at ASC LIMIT %s", (*args, limit)))


def billing_payment_row(payment_row_id: int) -> Optional[dict]:
    """La ligne de journal derrière une facture — ce que la reprise relit.

    Vit ici plutôt que dans `db/billing.py` parce que c'est la reprise de
    facturation qui la demande : le cycle de paiement, lui, a toujours sa ligne
    en main quand il en a besoin."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM billing_payments WHERE id = %s", (payment_row_id,)).fetchone()
