## prerequisite — a jefacture.com subscription that opens the API

oto reads **your own** jefacture.com account: you sign in with jefacture.com and authorize oto.

- the API is opened by the **Premium** or **Optimum** plan — not by Freemium
- the sign-in shows a **QR code**: scan it with the **jefacture.com mobile app**, then, the first time, consent to oto reading your spaces
- oto sees what **you** see: every space (company) you reach, a client's space included when you reach it as its accounting firm
- read-only: nothing is issued, sent, paid or changed
- the connection lasts until you remove it or jefacture.com revokes it; if it lapses, the card says « reconnect »

## setup — the application and the callback URL

operator only. Create an **application** on the Banqup developer portal (jefacture.com runs on the Banqup platform, French region), with the API products the reads need (documents, payments, network, payment accounts). Its **API key** is the `client_id`. Activate this redirect URI for the application, byte for byte:

{{callback:/api/jefacture/oauth/callback}}

then set the two values at platform scope, once per base:

- `oto_admin_connector_setting(op="set", connector="jefacture", key="client_id", value="…")`
- `oto_admin_connector_setting(op="set", connector="jefacture", key="client_secret", value="…")`

until they are set, the card stays visible and « connect » refuses **naming the missing key**. `client_secret` is a real secret: never returned by an API, never logged. Each instance has its own callback URL — activate each one.

## usage — spaces, invoices, what remains due

- `jefacture(op="list_spaces")` first: each space is one company, its `id` is the `space_id` of every other read
- `op="list_documents"` — invoices and credit notes: `folder=SALES` for issued, `PURCHASES` for received; `status_query="pay=UNPAID OR pay=PARTIALLY_PAID"` for what is still to be paid; what remains due is `openBalance`
- `op="get_document"` — one document with its lines; `list_balance_offsets` and `get_payment` — the payments imputed to it
- `op="list_reconciliations"` — documents with their matched, proposed or missing payments
- `op="list_connections"` / `get_connection` — customers and suppliers, with their legal identifiers
- `op="list_payment_accounts"` / `list_account_movements` — accounts, balances and movements

lists are paged from 0. An argument an op does not read is refused, never dropped.

by default the lists are tightened — ids, numbers, dates, amounts, statuses and counterparts stay, the bulky blocks another read serves leave: a document's included `connections` (its supplier and customer stay in `metadata`) and `balanceOffsets` (`list_balance_offsets`), a customer's or supplier's `addresses` and `metadata` (`get_connection`), an account's `availableOperations`, a movement's `balancesAfterOperation`. `full=True` returns them as jefacture.com sends them.

## note — what is not in jefacture.com

no bookkeeping entries, no balance sheet, no sales to private customers that never become an invoice: ask the accounting software for those.
