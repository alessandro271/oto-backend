CREATE TABLE IF NOT EXISTS billing_identities (
org_id BIGINT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
legal_name TEXT NOT NULL,
country_code TEXT NOT NULL,
vat_number TEXT,
address_line TEXT,
address_line2 TEXT,
postal_code TEXT,
city TEXT,
billing_email TEXT,
pennylane_customer_id BIGINT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
