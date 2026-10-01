CREATE TABLE IF NOT EXISTS outreach_sends (
id BIGSERIAL PRIMARY KEY,
campaign TEXT NOT NULL,
sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
to_email TEXT NOT NULL,
locale TEXT NOT NULL,
kind TEXT NOT NULL DEFAULT 'send',
fingerprint TEXT NOT NULL,
sent_by TEXT,
sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_once
ON outreach_sends(campaign, sub) WHERE kind = 'send';
CREATE INDEX IF NOT EXISTS idx_outreach_campaign ON outreach_sends(campaign, sent_at DESC);
CREATE TABLE IF NOT EXISTS outreach_optouts (
sub TEXT PRIMARY KEY REFERENCES users(sub) ON DELETE CASCADE,
source TEXT NOT NULL DEFAULT 'link',
opted_out_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS signal_digest_optouts (
sub TEXT PRIMARY KEY REFERENCES users(sub) ON DELETE CASCADE,
source TEXT NOT NULL DEFAULT 'link',
opted_out_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
