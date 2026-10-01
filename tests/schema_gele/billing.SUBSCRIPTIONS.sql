CREATE TABLE IF NOT EXISTS org_subscriptions (
org_id BIGINT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
provider TEXT NOT NULL DEFAULT 'mollie',
customer_id TEXT,
card_id TEXT,
sepa_id TEXT,
mandate_id TEXT,
mandate_rum TEXT,
method TEXT NOT NULL DEFAULT 'card',
plan TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'active',
current_period_end TIMESTAMPTZ,
next_billing_at TIMESTAMPTZ,
grace_until TIMESTAMPTZ,
canceled_at TIMESTAMPTZ,
block_code TEXT,
block_detail TEXT,
block_since TIMESTAMPTZ,
block_seen_at TIMESTAMPTZ,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_org_subs_due
ON org_subscriptions(next_billing_at) WHERE status IN ('active', 'past_due');
CREATE TABLE IF NOT EXISTS billing_payments (
id BIGSERIAL PRIMARY KEY,
org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
kind TEXT NOT NULL,
amount INTEGER NOT NULL,
currency TEXT NOT NULL DEFAULT 'eur',
amount_ht INTEGER,
vat_rate_bps INTEGER,
vat_amount INTEGER,
country_code TEXT,
vat_scheme TEXT,
payment_intent_id TEXT,
payment_id TEXT,
customer_id TEXT,
status TEXT NOT NULL,
attempt SMALLINT NOT NULL DEFAULT 1,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_billing_payments_org ON billing_payments(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_billing_payments_open
ON billing_payments(created_at) WHERE status NOT IN ('paid', 'failed', 'canceled', 'expired');
