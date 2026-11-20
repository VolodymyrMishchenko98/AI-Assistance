CREATE TABLE IF NOT EXISTS import_operations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(id),
    idempotency_key TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    final_fingerprint TEXT,
    status TEXT NOT NULL CHECK (status IN ('uploaded', 'preview_ready', 'confirmed', 'expired', 'failed')),
    selected_sheet TEXT,
    header_row INTEGER,
    sheets_json TEXT NOT NULL CHECK (json_valid(sheets_json)),
    created_item_ids_json TEXT NOT NULL CHECK (json_valid(created_item_ids_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS import_preview_rows (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES import_operations(id) ON DELETE CASCADE,
    excel_row_number INTEGER NOT NULL,
    raw_values_json TEXT NOT NULL CHECK (json_valid(raw_values_json)),
    name TEXT,
    quantity TEXT,
    unit TEXT,
    description TEXT,
    errors_json TEXT NOT NULL CHECK (json_valid(errors_json)),
    warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json)),
    status TEXT NOT NULL CHECK (status IN ('valid', 'invalid', 'warning', 'excluded')),
    UNIQUE(operation_id, excel_row_number)
);

CREATE INDEX IF NOT EXISTS idx_import_operations_expires_at ON import_operations(expires_at);
CREATE INDEX IF NOT EXISTS idx_import_preview_rows_operation ON import_preview_rows(operation_id);
