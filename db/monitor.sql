CREATE TABLE IF NOT EXISTS monitor_watches (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    sku TEXT NOT NULL,
    color TEXT NOT NULL,
    size TEXT NOT NULL,
    label TEXT NOT NULL,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    product_key TEXT NOT NULL,
    variant_key TEXT NOT NULL,
    offer_key TEXT NOT NULL,
    retailer_key TEXT NOT NULL DEFAULT 'arcteryx-official',
    retailer_name TEXT NOT NULL DEFAULT 'Arc''teryx official',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    rule TEXT NOT NULL CHECK (rule IN ('target','new_low','restock')),
    target_price_minor INTEGER,
    interval_minutes INTEGER NOT NULL CHECK (interval_minutes BETWEEN 30 AND 1440),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'never_checked',
    last_error TEXT,
    last_price_minor INTEGER,
    last_availability TEXT,
    last_explicit_availability TEXT,
    next_due_at TEXT,
    armed INTEGER NOT NULL DEFAULT 1 CHECK (armed IN (0,1)),
    event_number INTEGER NOT NULL DEFAULT 0,
    event_threshold_minor INTEGER,
    lease_token TEXT,
    lease_until TEXT,
    config_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (url,sku)
);

CREATE TABLE IF NOT EXISTS monitor_checks (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES monitor_watches(id),
    checked_at TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('primary','confirmation')),
    status TEXT NOT NULL,
    error TEXT,
    quote_json TEXT
);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES monitor_watches(id),
    event_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    price_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    rule TEXT NOT NULL,
    url TEXT NOT NULL,
    label TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    UNIQUE (watch_id,event_number)
);

CREATE INDEX IF NOT EXISTS monitor_due ON monitor_watches(enabled,next_due_at);
CREATE INDEX IF NOT EXISTS monitor_checks_watch ON monitor_checks(watch_id,id);
