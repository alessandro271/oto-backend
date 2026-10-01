CREATE TABLE IF NOT EXISTS billing_invoices (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
payment_row_id BIGINT NOT NULL REFERENCES billing_payments(id) ON DELETE CASCADE,
payment_ref TEXT,
kind TEXT NOT NULL DEFAULT 'invoice',
status TEXT NOT NULL DEFAULT 'pending',
external_reference TEXT,
pennylane_customer_id BIGINT,
pennylane_invoice_id BIGINT,
credited_invoice_id BIGINT,
number TEXT,
currency TEXT NOT NULL DEFAULT 'eur',
amount_ht INTEGER,
vat_rate_bps INTEGER,
vat_amount INTEGER,
amount_ttc INTEGER,
vat_scheme TEXT,
period_start TIMESTAMPTZ,
period_end TIMESTAMPTZ,
issued_at TIMESTAMPTZ,
pdf BYTEA,
pdf_filename TEXT,
pdf_url TEXT,
emailed_at TIMESTAMPTZ,
email_to TEXT,
attempts SMALLINT NOT NULL DEFAULT 0,
error_code TEXT,
error_detail TEXT,
last_attempt_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE (payment_row_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_billing_invoices_org
ON billing_invoices(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_billing_invoices_pending
ON billing_invoices(created_at) WHERE status = 'pending';
