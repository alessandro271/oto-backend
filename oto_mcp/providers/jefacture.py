"""Registry declaration of the `jefacture` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import _c

# jefacture: French e-invoicing, READ-ONLY — issued and received invoices and their
# statuses, what remains due and the payments matched to it, customers and
# suppliers, payment accounts and their movements. jefacture.com runs on the Banqup
# platform (French region): the calls reach Banqup's API, hence the publisher.
#
# ON BEHALF OF THE PERSON: each one signs in with their own jefacture.com account
# (OAuth 2.0 authorization code, `auth/jefacture.py`) and the token reads what THEY
# reach — possibly several spaces (companies). The OAuth application is the
# platform's: `client_id` and `client_secret` at PLATFORM scope of
# `connector_settings`, set by the operator. No credential field: the stored
# secret is the refresh token. The host is fixed: no egress guard to set.
CONNECTOR = _c(
    "jefacture", ["jefacture"],
    auth_modes={"byo_user"},
    # Consent comes from the person's jefacture.com account, not from their org.
    personal_session=True, secret_kind="oauth",
    label="jefacture.com",
    help="French e-invoicing with your jefacture.com account, read-only — invoices "
         "issued and received, what remains due, payments, customers and suppliers",
    href="https://www.jefacture.com",
)

CATEGORY = "Finance"
PUBLISHER = "Banqup"
LOGO_DOMAIN = "jefacture.com"

DESCRIPTION = (
    "Read your jefacture.com e-invoicing: invoices and credit notes issued and "
    "received with their statuses, what remains due, the payments matched to them, "
    "customers and suppliers, payment accounts with their balances and movements. "
    "You sign in with jefacture.com; read-only."
)
