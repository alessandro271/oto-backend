CREATE TABLE IF NOT EXISTS credit_scales (
version BIGINT PRIMARY KEY CHECK (version > 0),
posed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS credit_scale_lines (
version BIGINT NOT NULL REFERENCES credit_scales(version) ON DELETE CASCADE,
connector TEXT NOT NULL,
function TEXT NOT NULL,
unit TEXT NOT NULL,
budget BIGINT NOT NULL CHECK (budget >= 0),
per_n BIGINT NOT NULL DEFAULT 1 CHECK (per_n > 0),
PRIMARY KEY (version, connector, function, unit)
);
CREATE TABLE IF NOT EXISTS credit_payer_modes (
org_id INTEGER PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
mode TEXT NOT NULL CHECK (mode IN ('org', 'person')),
set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS credit_balances (
payer TEXT PRIMARY KEY,
balance BIGINT NOT NULL DEFAULT 0,
holds_open BIGINT NOT NULL DEFAULT 0 CHECK (holds_open >= 0),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS credit_holds (
call_uid TEXT PRIMARY KEY,
payer TEXT NOT NULL,
connector TEXT NOT NULL,
function TEXT NOT NULL,
unit TEXT NOT NULL,
scale_version BIGINT NOT NULL,
quantity BIGINT NOT NULL CHECK (quantity >= 0),
reserved BIGINT NOT NULL CHECK (reserved >= 0),
deadline TIMESTAMPTZ NOT NULL,
job JSONB,
state TEXT NOT NULL DEFAULT 'open'
CHECK (state IN ('open', 'settled', 'released', 'expired')),
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_credit_holds_ouvertes
ON credit_holds(deadline) WHERE state = 'open';
CREATE TABLE IF NOT EXISTS credit_movements (
id BIGSERIAL PRIMARY KEY,
xid XID8 NOT NULL DEFAULT pg_current_xact_id(),
payer TEXT NOT NULL,
kind TEXT NOT NULL CHECK (kind IN ('deposit', 'settle', 'transfer')),
amount BIGINT NOT NULL DEFAULT 0,
held BIGINT NOT NULL DEFAULT 0,
reason TEXT,
idempotency_key TEXT,
call_uid TEXT,
connector TEXT,
function TEXT,
quantity BIGINT,
meta JSONB NOT NULL DEFAULT '{}'::jsonb,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_movements_idempotence
ON credit_movements(payer, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_credit_movements_curseur
ON credit_movements(xid, id);
