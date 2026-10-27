"""SQLite storage using only the Python standard library.

An import is one JSON object with an ``observations`` array. See import's
REQUIRED_FIELDS below; all descriptive identity fields are immutable. Use a
different product_key for a different model/season or construction revision.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA = Path(__file__).resolve().parents[2] / 'db' / 'schema.sql'
UTC = timezone.utc
REQUIRED_FIELDS = {
    'product_key', 'name', 'model_code', 'season', 'variant_key', 'color', 'size',
    'retailer_key', 'retailer_name', 'offer_key', 'market', 'currency',
    'observed_at', 'captured_at', 'price_minor', 'availability',
    'product_condition', 'price_type', 'source_type', 'source_url',
}


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError('timestamps must be ISO 8601 strings with a UTC offset')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'invalid ISO 8601 timestamp: {value}') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamps must include a UTC offset')
    return parsed.astimezone(UTC)


def canonical_time(value: str | datetime) -> str:
    parsed = parse_time(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamps must include a UTC offset')
    return parsed.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def initialize(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, 1):
        connection.close()
        raise ValueError(f'unsupported database schema version {version}')
    connection.executescript(SCHEMA.read_text(encoding='utf-8'))
    return connection


def _identity(connection, table, identity, descriptions):
    where = ' AND '.join(f'{key} = ?' for key in identity)
    row = connection.execute(f'SELECT * FROM {table} WHERE {where}', tuple(identity.values())).fetchone()
    if row:
        if any(row[key] != value for key, value in descriptions.items()):
            raise ValueError(f'conflicting {table} identity: {identity}')
        return row['id']
    values = identity | descriptions
    columns = ', '.join(values)
    placeholders = ', '.join('?' for _ in values)
    cursor = connection.execute(
        f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(values.values())
    )
    return cursor.lastrowid


def _validate(record):
    if not isinstance(record, dict):
        raise ValueError('each observation must be a JSON object')
    missing = REQUIRED_FIELDS - record.keys()
    extra = record.keys() - REQUIRED_FIELDS - {'notes'}
    if missing or extra:
        raise ValueError(f'observation fields: missing={sorted(missing)}, unknown={sorted(extra)}')
    for key in REQUIRED_FIELDS - {'price_minor'}:
        if not isinstance(record[key], str) or not record[key].strip():
            raise ValueError(f'{key} must be a nonempty string')
    if type(record['price_minor']) is not int or not (0 < record['price_minor'] <= 2**63 - 1):
        raise ValueError('price_minor must be a positive 64-bit integer number of cents')
    if not isinstance(record.get('notes', ''), str):
        raise ValueError('notes must be a string')
    for key, allowed in {
        'availability': {'in_stock', 'out_of_stock', 'unknown'},
        'product_condition': {'new', 'used', 'refurbished'},
        'price_type': {'public', 'member', 'coupon', 'bundle'},
        'source_type': {'retailer_page', 'authorized_feed', 'manual', 'archive'},
    }.items():
        if record[key] not in allowed:
            raise ValueError(f'invalid {key}: {record[key]}')
    if (record['market'], record['currency']) not in {('US', 'USD'), ('CA', 'CAD')}:
        raise ValueError('supported market/currency pairs are US/USD and CA/CAD')
    url = urlsplit(record['source_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('source_url must be an HTTPS URL without embedded credentials')
    normalized = record | {
        'observed_at': canonical_time(record['observed_at']),
        'captured_at': canonical_time(record['captured_at']),
        'notes': record.get('notes', ''),
    }
    if normalized['observed_at'] > normalized['captured_at']:
        raise ValueError('captured_at cannot precede observed_at')
    return normalized


def import_observations(connection: sqlite3.Connection, payload: dict) -> dict:
    """Atomically import records. Duplicate imports do not change source history."""
    if not isinstance(payload, dict) or set(payload) != {'observations'}:
        raise ValueError('input must contain exactly one observations array')
    if not isinstance(payload['observations'], list):
        raise ValueError('observations must be an array')
    inserted = duplicates = 0
    with connection:
        for index, record in enumerate(payload['observations']):
            try:
                r = _validate(record)
                product = _identity(connection, 'products', {'product_key': r['product_key']}, {
                    'name': r['name'], 'model_code': r['model_code'], 'season': r['season'],
                })
                variant = _identity(connection, 'variants', {
                    'product_id': product, 'variant_key': r['variant_key'],
                }, {'color': r['color'], 'size': r['size']})
                retailer = _identity(connection, 'retailers', {'retailer_key': r['retailer_key']}, {
                    'name': r['retailer_name'],
                })
                offer = _identity(connection, 'offers', {
                    'retailer_id': retailer, 'offer_key': r['offer_key'],
                    'market': r['market'], 'currency': r['currency'],
                }, {'variant_id': variant})
                observation_identity = {
                    'offer_id': offer, 'observed_at': r['observed_at'], 'source_url': r['source_url'],
                }
                description = {key: r[key] for key in (
                    'price_minor', 'availability', 'product_condition', 'price_type',
                    'source_type', 'notes',
                )}
                old = connection.execute(
                    'SELECT * FROM price_observations WHERE offer_id=? AND observed_at=? AND source_url=?',
                    tuple(observation_identity.values()),
                ).fetchone()
                if old:
                    if any(old[key] != value for key, value in description.items()):
                        raise ValueError('conflicting price observation; original record retained')
                    # A later retrieval of the same historical observation is a
                    # duplicate. Preserve the original captured_at audit value.
                    duplicates += 1
                    continue
                _identity(connection, 'price_observations', observation_identity,
                          description | {'captured_at': r['captured_at']})
                inserted += 1
            except (ValueError, sqlite3.IntegrityError) as exc:
                raise ValueError(f'observation[{index}]: {exc}') from exc
    return {'inserted': inserted, 'duplicates': duplicates}


def metrics(connection: sqlite3.Connection, *, product_key: str, variant_key: str,
            market: str, currency: str, as_of: str | None = None,
            retailer_key: str | None = None, offer_key: str | None = None) -> dict:
    """Metrics for comparable new, in-stock, public prices at a given UTC time.

    Windows contain 365/90 UTC calendar dates including as_of's date. No record
    after as_of contributes. Missing dates are not filled or treated as zero.
    Minima describe only recorded observations, never proven lifetime lows.
    """
    if (market, currency) not in {('US', 'USD'), ('CA', 'CAD')}:
        raise ValueError('supported market/currency pairs are US/USD and CA/CAD')
    if offer_key is not None and retailer_key is None:
        raise ValueError('offer_key requires retailer_key to identify a retailer-scoped offer')
    end = parse_time(as_of) if as_of else datetime.now(UTC)
    start365 = datetime.combine(end.date() - timedelta(days=364), time.min, UTC)
    start90 = datetime.combine(end.date() - timedelta(days=89), time.min, UTC)
    end_string = canonical_time(end)
    rows = connection.execute('''
        SELECT po.*, o.offer_key, r.retailer_key, r.name AS retailer_name
        FROM price_observations po
        JOIN offers o ON o.id = po.offer_id
        JOIN retailers r ON r.id = o.retailer_id
        JOIN variants v ON v.id = o.variant_id
        JOIN products p ON p.id = v.product_id
        WHERE p.product_key = ? AND v.variant_key = ? AND o.market = ? AND o.currency = ?
          AND po.observed_at <= ? AND po.product_condition = 'new'
          AND po.availability = 'in_stock' AND po.price_type = 'public'
          AND (? IS NULL OR r.retailer_key = ?)
          AND (? IS NULL OR o.offer_key = ?)
        ORDER BY po.observed_at, po.id
    ''', (product_key, variant_key, market, currency, end_string,
          retailer_key, retailer_key, offer_key, offer_key)).fetchall()
    start365_string, start90_string = canonical_time(start365), canonical_time(start90)
    rows365 = [row for row in rows if row['observed_at'] >= start365_string]
    rows90 = [row for row in rows if row['observed_at'] >= start90_string]
    daily = {}
    for row in rows90:
        day = row['observed_at'][:10]
        daily[day] = min(daily.get(day, row['price_minor']), row['price_minor'])

    def minimum(records):
        if not records:
            return None
        low = min(records, key=lambda row: (row['price_minor'], row['observed_at'], row['id']))
        return {key: low[key] for key in (
            'price_minor', 'observed_at', 'captured_at', 'retailer_key', 'offer_key',
            'source_type', 'source_url',
        )}

    average = (Decimal(sum(daily.values())) / len(daily)) if daily else None
    return {
        'scope': {'product_key': product_key, 'variant_key': variant_key, 'market': market,
                  'currency': currency, 'condition': 'new', 'availability': 'in_stock',
                  'price_type': 'public', 'tax_shipping_included': False,
                  'retailer_key': retailer_key, 'offer_key': offer_key},
        'as_of': end_string,
        'window_policy': 'UTC calendar days including as_of date; observations after as_of excluded',
        'history': {'first_observed_at': rows[0]['observed_at'] if rows else None,
                    'last_observed_at': rows[-1]['observed_at'] if rows else None,
                    'observation_count': len(rows),
                    'observed_days': len({row['observed_at'][:10] for row in rows}),
                    'proven_lifetime_history': False},
        'observed_all_time_min': minimum(rows),
        'lowest_365d': minimum(rows365),
        'coverage_365d': {'window_start': start365_string, 'expected_days': 365,
                         'observed_days': len({row['observed_at'][:10] for row in rows365}),
                         'observation_count': len(rows365)},
        'average_daily_low_90d': {
            'method': 'arithmetic mean of UTC daily minimum prices across eligible retailers',
            'window_start': start90_string, 'expected_days': 90,
            'observed_days': len(daily), 'missing_days': 90 - len(daily),
            'observation_count': len(rows90), 'complete_day_coverage': len(daily) == 90,
            'sum_daily_min_minor': sum(daily.values()) if daily else None,
            'mean_minor': str(average.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)) if average is not None else None,
            'display_minor': int(average.quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if average is not None else None,
            'daily_minima': [{'day': day, 'price_minor': price} for day, price in sorted(daily.items())],
        },
        'limitations': [
            'Recorded minima are not proof of the lowest price ever sold.',
            'Missing dates are excluded from the mean; no zero or forward-fill imputation.',
            'The as_of UTC date can be incomplete; full day coverage does not prove continuous monitoring.',
            'Prices exclude tax, shipping, coupons, memberships, bundles, and used goods.',
        ],
    }


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))
