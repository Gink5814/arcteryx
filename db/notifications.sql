-- The run-once file lock serializes delivery; credentials never enter this table.
CREATE TABLE IF NOT EXISTS notification_deliveries (
    event_key TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('price', 'health')),
    watch_id INTEGER NOT NULL REFERENCES monitor_watches(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'expired')),
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT,
    sent_at TEXT,
    last_error TEXT,
    PRIMARY KEY (event_key, recipient)
);
