CREATE TABLE IF NOT EXISTS nodes (
id BIGSERIAL PRIMARY KEY,
public_id TEXT NOT NULL,
parent_id BIGINT,
position BIGINT,
kind TEXT NOT NULL,
owner_type TEXT NOT NULL,
owner_id TEXT NOT NULL,
props JSONB NOT NULL DEFAULT '{}'::jsonb,
data JSONB NOT NULL DEFAULT '{}'::jsonb,
claimed_by TEXT,
claimed_until TIMESTAMPTZ,
claimed_run TEXT,
claims INTEGER NOT NULL DEFAULT 0,
abandon_reason TEXT,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT nodes_public_id_key UNIQUE (public_id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_owner_scoped ON nodes(owner_type, owner_id)
WHERE kind <> 'ligne';
CREATE TABLE IF NOT EXISTS blocks (
id BIGSERIAL PRIMARY KEY,
public_id TEXT NOT NULL,
node_id BIGINT NOT NULL,
position BIGINT NOT NULL,
type TEXT NOT NULL,
props JSONB NOT NULL DEFAULT '{}'::jsonb,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
CONSTRAINT blocks_public_id_key UNIQUE (public_id),
CONSTRAINT blocks_node_fk FOREIGN KEY (node_id)
REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_blocks_node ON blocks(node_id, position);
