PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    product_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    model_code TEXT NOT NULL,
    season TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT 'Arc''teryx'
);

CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    variant_key TEXT NOT NULL,
    color TEXT NOT NULL,
    size TEXT NOT NULL,
    UNIQUE (product_id, variant_key)
);

CREATE TABLE IF NOT EXISTS retailers (
    id INTEGER PRIMARY KEY,
    retailer_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    variant_id INTEGER NOT NULL REFERENCES variants(id),
    retailer_id INTEGER NOT NULL REFERENCES retailers(id),
    offer_key TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('US', 'CA')),
    currency TEXT NOT NULL CHECK (currency IN ('USD', 'CAD')),
    CHECK ((market = 'US' AND currency = 'USD') OR
           (market = 'CA' AND currency = 'CAD')),
    UNIQUE (retailer_id, offer_key, market, currency)
);

-- Raw observations are append-only via the importer. A repeated source record
-- is idempotent; a conflicting value at that identity is rejected, never updated.
-- Prices are public unit merchandise prices, excluding tax and shipping.
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    observed_at TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    price_minor INTEGER NOT NULL CHECK (typeof(price_minor) = 'integer' AND price_minor > 0),
    availability TEXT NOT NULL CHECK (availability IN ('in_stock', 'out_of_stock', 'unknown')),
    product_condition TEXT NOT NULL CHECK (product_condition IN ('new', 'used', 'refurbished')),
    price_type TEXT NOT NULL CHECK (price_type IN ('public', 'member', 'coupon', 'bundle')),
    source_type TEXT NOT NULL CHECK (source_type IN ('retailer_page', 'authorized_feed', 'manual', 'archive')),
    source_url TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE (offer_id, observed_at, source_url)
);

CREATE INDEX IF NOT EXISTS observations_offer_time
    ON price_observations (offer_id, observed_at);
CREATE INDEX IF NOT EXISTS offers_variant_market
    ON offers (variant_id, market, currency);
PRAGMA user_version = 1;
