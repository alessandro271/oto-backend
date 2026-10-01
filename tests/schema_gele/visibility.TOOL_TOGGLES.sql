CREATE TABLE IF NOT EXISTS user_disabled_tools (
sub TEXT NOT NULL,
org_id BIGINT NOT NULL DEFAULT 0,
tool_name TEXT NOT NULL,
disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (sub, org_id, tool_name)
);
CREATE TABLE IF NOT EXISTS user_enabled_tools (
sub TEXT NOT NULL,
org_id BIGINT NOT NULL DEFAULT 0,
tool_name TEXT NOT NULL,
enabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (sub, org_id, tool_name)
);
CREATE TABLE IF NOT EXISTS org_disabled_tools (
org_id BIGINT NOT NULL,
tool_name TEXT NOT NULL,
disabled_by TEXT,
disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (org_id, tool_name)
);
CREATE TABLE IF NOT EXISTS group_disabled_tools (
group_id BIGINT NOT NULL,
tool_name TEXT NOT NULL,
disabled_by TEXT,
disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
PRIMARY KEY (group_id, tool_name)
);
