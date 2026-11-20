CREATE TABLE IF NOT EXISTS review_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('draft', 'running', 'cancelled', 'completed')),
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
    policy_required_fields TEXT NOT NULL CHECK (json_valid(policy_required_fields)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procurement_items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(id),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT,
    quantity TEXT CHECK (quantity IS NULL OR (length(trim(quantity)) > 0 AND CAST(quantity AS REAL) > 0)),
    unit TEXT,
    status TEXT NOT NULL CHECK (status IN ('draft', 'pending', 'failed', 'completed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    data_json TEXT NOT NULL CHECK (json_valid(data_json)),
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_procurement_items_run_id ON procurement_items(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_aggregate ON audit_events(aggregate_type, aggregate_id);

