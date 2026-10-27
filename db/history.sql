-- Historical reports are references until a primary archived page is independently
-- fetched and parsed for an exact watched variant. No implicit metric inclusion.
CREATE TABLE IF NOT EXISTS historical_evidence (
    id INTEGER PRIMARY KEY,
    evidence_key TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observation_id INTEGER REFERENCES price_observations(id),
    watch_id INTEGER REFERENCES monitor_watches(id),
    verification_json TEXT,
    UNIQUE (source_url, observed_at)
);
CREATE INDEX IF NOT EXISTS historical_evidence_date ON historical_evidence(observed_at);
