"""SQLite schema definitions for Merchant FinPilot.

PROJECT_RULES 4.2, 10.7, 10.9 / ARCHITECTURE.md §15, §16.

Key invariants enforced in DDL:
- All monetary amounts stored as INTEGER paise (minor units).
- Idempotency keys enforced via UNIQUE constraints (incident_key, audit sequence).
- Timestamps stored as ISO-8601 UTC strings.
- Enums stored as text.
"""

SCHEMA_DDL = """
-- Payments table
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    amount_paise INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    created_at TEXT NOT NULL,
    error_code TEXT,
    error_description TEXT,
    error_source TEXT,
    error_step TEXT,
    error_reason TEXT,
    region TEXT,
    provider TEXT,
    failure_category TEXT,
    source_confidence TEXT
);

CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_method ON payments(method);
CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);

-- Incidents table
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    incident_key TEXT UNIQUE NOT NULL,
    merchant_id TEXT NOT NULL,
    incident_type TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    primary_dimension TEXT,
    primary_dimension_value TEXT,
    metrics_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_merchant ON incidents(merchant_id);
CREATE INDEX IF NOT EXISTS idx_incidents_detected_at ON incidents(detected_at);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

-- Financial Evidence table
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    source_confidence TEXT NOT NULL,
    dimension TEXT,
    metrics_json TEXT,
    breakdown_json TEXT,
    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence(incident_id);

-- Investigation Reports table
CREATE TABLE IF NOT EXISTS investigations (
    incident_id TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    investigated_at TEXT NOT NULL,
    has_sufficient_evidence INTEGER NOT NULL,
    has_multiple_concentrations INTEGER NOT NULL,
    summary TEXT NOT NULL,
    report_json TEXT NOT NULL,
    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id) ON DELETE CASCADE
);

-- Audit Events table (Append-only)
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    sequence INTEGER UNIQUE NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    incident_id TEXT,
    subject_id TEXT,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_sequence ON audit_events(sequence);
CREATE INDEX IF NOT EXISTS idx_audit_incident ON audit_events(incident_id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);
"""
