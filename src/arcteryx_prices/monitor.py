"""Single-user local watch scheduling and twice-verified, persistent site alerts.

Fetch failures never synthesize prices, stock changes, or a successful check.
All public prices remain scoped to one retailer URL and exact variant SKU.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import canonical_time, import_observations, initialize, metrics, parse_time


MONITOR_SCHEMA = Path(__file__).resolve().parents[2] / 'db' / 'monitor.sql'
UTC = timezone.utc


def _now(value=None):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        return parse_time(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('now must be an aware datetime or ISO timestamp')
    return value.astimezone(UTC)


def _validate_url(url):
    from .collector import CollectorError, validate_product_url
    try:
        return validate_product_url(url)
    except CollectorError as exc:
        raise ValueError(str(exc)) from exc


def _fetcher():
    from .collector import collect_quote
    return collect_quote


def initialize_monitor(db_path):
    connection = initialize(db_path)
    connection.execute('PRAGMA busy_timeout = 10000')
    connection.executescript(MONITOR_SCHEMA.read_text(encoding='utf-8'))
    # Additive migration preserves existing official watch and price identities.
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(monitor_watches)')}
    for name, declaration in (('retailer_key', "TEXT NOT NULL DEFAULT 'arcteryx-official'"),
                              ('retailer_name', "TEXT NOT NULL DEFAULT 'Arc''teryx official'")):
        if name not in columns:
            try:
                connection.execute(f'ALTER TABLE monitor_watches ADD COLUMN {name} {declaration}')
            except sqlite3.OperationalError:
                if name not in {row['name'] for row in connection.execute('PRAGMA table_info(monitor_watches)')}:
                    raise
    from .history import initialize_history
    initialize_history(connection)
    return connection


def _text(value, field, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{field} must be a nonempty string up to {limit} characters')
    return value.strip()


def _settings(payload, *, creating=False):
    result = {}
    if creating or 'rule' in payload:
        rule = payload.get('rule', 'target')
        if rule not in {'target', 'new_low', 'restock'}:
            raise ValueError('rule must be target, new_low, or restock')
        result['rule'] = rule
    if creating or 'target_price_minor' in payload:
        price = payload.get('target_price_minor')
        if price is not None and (type(price) is not int or not (0 < price <= 2**63 - 1)):
            raise ValueError('target_price_minor must be a positive 64-bit integer or null')
        result['target_price_minor'] = price
    if creating or 'interval_minutes' in payload:
        interval = payload.get('interval_minutes', 60)
        if type(interval) is not int or not 30 <= interval <= 1440:
            raise ValueError('interval_minutes must be an integer from 30 to 1440')
        result['interval_minutes'] = interval
    if 'enabled' in payload:
        if type(payload['enabled']) is not bool:
            raise ValueError('enabled must be a boolean')
        result['enabled'] = int(payload['enabled'])
    return result


def _row(connection, watch_id):
    if type(watch_id) is not int or watch_id <= 0:
        raise ValueError('watch id must be a positive integer')
    row = connection.execute('SELECT * FROM monitor_watches WHERE id=?', (watch_id,)).fetchone()
    if row is None:
        raise ValueError('watch not found')
    return dict(row)


def _public(connection, row, now=None):
    instant = _now(now)
    fresh = (row['enabled'] and row['last_status'] == 'ok' and row['last_success_at'] is not None
             and parse_time(row['last_success_at']) <= instant
             and instant <= parse_time(row['last_success_at']) + timedelta(minutes=row['interval_minutes']))
    result = {key: value for key, value in row.items() if key not in {'lease_token', 'lease_until'}}
    result['enabled'] = bool(row['enabled'])
    result['stale'] = not bool(fresh)
    result['status'] = row['last_status'] if row['enabled'] else 'paused'
    result['availability'] = row['last_availability']
    result['current_price_minor'] = row['last_price_minor'] if fresh and row['last_availability'] == 'in_stock' else None
    result['current_price'] = result['current_price_minor']
    result['last_checked'] = row['last_checked_at']
    result['last_success'] = row['last_success_at']
    result['checking'] = bool(row['lease_until'] and parse_time(row['lease_until']) > instant)
    result['metrics'] = metrics(
        connection, product_key=row['product_key'], variant_key=row['variant_key'],
        market=row['market'], currency=row['currency'], as_of=canonical_time(instant),
        retailer_key=row['retailer_key'], offer_key=row['offer_key'],
    )
    return result


def create_watch(connection, payload):
    if not isinstance(payload, dict):
        raise ValueError('watch must be an object')
    allowed = {'url', 'sku', 'color', 'size', 'label', 'target_price_minor', 'rule', 'interval_minutes'}
    if set(payload) - allowed:
        raise ValueError(f'unknown watch fields: {sorted(set(payload)-allowed)}')
    url = _validate_url(_text(payload.get('url'), 'url', 2000))
    sku, color, size = (_text(payload.get(key), key) for key in ('sku', 'color', 'size'))
    label = _text(payload.get('label', f"Arc'teryx {sku} / {color} / {size}"), 'label')
    settings = _settings(payload, creating=True)
    if settings['rule'] == 'target' and settings['target_price_minor'] is None:
        raise ValueError('target rule requires target_price_minor')
    digest = hashlib.sha256((url['url'] + '\n' + sku).encode()).hexdigest()
    stamp = canonical_time(_now())
    retailer_key = url.get('retailer_key', 'arcteryx-official')
    retailer_name = url.get('retailer_name', "Arc'teryx official")
    product_prefix = 'official' if retailer_key == 'arcteryx-official' else retailer_key
    values = {
        'url': url['url'], 'sku': sku, 'color': color, 'size': size, 'label': label,
        'market': url['market'], 'currency': url['currency'],
        'retailer_key': retailer_key, 'retailer_name': retailer_name,
        'product_key': f'{product_prefix}:{digest}', 'variant_key': f'variant:{digest}',
        'offer_key': f'offer:{digest}', 'created_at': stamp, 'updated_at': stamp,
    } | settings
    try:
        with connection:
            cursor = connection.execute(
                f"INSERT INTO monitor_watches ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError('this retailer URL and SKU are already watched') from exc
    return _public(connection, _row(connection, cursor.lastrowid))


def list_watches(connection, now=None):
    return [_public(connection, dict(row), now) for row in connection.execute('SELECT * FROM monitor_watches ORDER BY id')]


def update_watch(connection, watch_id, payload):
    if not isinstance(payload, dict) or set(payload) - {'enabled', 'target_price_minor', 'rule', 'interval_minutes'}:
        raise ValueError('only enabled, target_price_minor, rule, and interval_minutes can be updated')
    settings = _settings(payload)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        old = _row(connection, watch_id)
        merged = old | settings
        if merged['rule'] == 'target' and merged['target_price_minor'] is None:
            raise ValueError('target rule requires target_price_minor')
        if settings:
            if any(key in settings and settings[key] != old[key] for key in ('rule', 'target_price_minor')):
                settings |= {'armed': 1, 'event_threshold_minor': None}
            if 'interval_minutes' in settings and old['last_checked_at']:
                settings['next_due_at'] = canonical_time(
                    parse_time(old['last_checked_at']) + timedelta(minutes=settings['interval_minutes']))
            if 'enabled' in settings and settings['enabled'] and not old['enabled']:
                settings['next_due_at'] = None
            settings |= {'updated_at': canonical_time(_now()), 'config_version': old['config_version'] + 1}
            connection.execute(
                f"UPDATE monitor_watches SET {','.join(f'{key}=?' for key in settings)} WHERE id=?",
                tuple(settings.values()) + (watch_id,),
            )
    return _public(connection, _row(connection, watch_id))


def list_alerts(connection):
    return [dict(row) for row in connection.execute('SELECT * FROM monitor_alerts ORDER BY id DESC')]


def _quote(watch, value):
    if not isinstance(value, dict):
        raise ValueError('collector did not return a quote object')
    required = {'price_minor', 'availability', 'observed_at', 'captured_at', 'source_url', 'evidence_sha256'}
    if required - value.keys():
        raise ValueError('collector quote is missing required fields')
    result = {key: value[key] for key in required}
    price = result['price_minor']
    if price is not None and (type(price) is not int or not 0 < price <= 2**63 - 1):
        raise ValueError('collector price must be positive integer cents')
    if result['availability'] not in {'in_stock', 'out_of_stock', 'unknown'}:
        raise ValueError('collector stock state is invalid')
    if result['availability'] == 'in_stock' and price is None:
        raise ValueError('in-stock quote requires a price')
    result['observed_at'] = canonical_time(result['observed_at'])
    result['captured_at'] = canonical_time(result['captured_at'])
    if result['observed_at'] > result['captured_at']:
        raise ValueError('quote capture precedes price observation')
    if not isinstance(result['evidence_sha256'], str) or len(result['evidence_sha256']) != 64:
        raise ValueError('quote requires a SHA-256 evidence digest')
    try:
        int(result['evidence_sha256'], 16)
    except ValueError as exc:
        raise ValueError('quote evidence digest is not hexadecimal') from exc
    location = _validate_url(result['source_url'])
    if any(location[key] != watch[key] for key in ('url', 'market', 'currency')):
        raise ValueError('quote source URL or market does not match the watch')
    result['source_url'] = location['url']
    for key in ('sku', 'color', 'size', 'market', 'currency'):
        if key in value and value[key] != watch[key]:
            raise ValueError(f'quote {key} does not match the watched variant')
    return result


def _collect(connection, watch, fetcher, stage, instant):
    try:
        quote = _quote(watch, fetcher(dict(watch)))
        entry = {'stage': stage, 'status': 'ok', 'error': None, 'quote': quote}
    except Exception as exc:
        status = getattr(exc, 'status', None)
        if not isinstance(status, str) or not re.fullmatch('[a-z_]{1,64}', status):
            status = 'parse_error' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'network_error'
        entry = {'stage': stage, 'status': status, 'error': str(exc)[:500], 'quote': None}
    with connection:
        connection.execute('''INSERT INTO monitor_checks
            (watch_id,checked_at,stage,status,error,quote_json) VALUES (?,?,?,?,?,?)''',
            (watch['id'], canonical_time(instant), stage, entry['status'], entry['error'],
             json.dumps(entry['quote'], sort_keys=True) if entry['quote'] else None))
    return entry


class _NoCommitConnection:
    """Let the existing importer participate in our event/observation transaction."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _observation(watch, quote):
    return {
        'product_key': watch['product_key'],
        'name': (f"Arc'teryx official variant {watch['sku']}" if watch['retailer_key'] == 'arcteryx-official'
                 else f"Arc'teryx {watch['retailer_name']} variant {watch['sku']}"),
        'model_code': watch['sku'], 'season': 'unverified',
        'variant_key': watch['variant_key'], 'color': watch['color'], 'size': watch['size'],
        'retailer_key': watch['retailer_key'], 'retailer_name': watch['retailer_name'],
        'offer_key': watch['offer_key'], 'market': watch['market'], 'currency': watch['currency'],
        'observed_at': quote['observed_at'], 'captured_at': quote['captured_at'],
        'price_minor': quote['price_minor'], 'availability': quote['availability'],
        'product_condition': 'new', 'price_type': 'public', 'source_type': 'retailer_page',
        'source_url': quote['source_url'],
        'notes': ('Official' if watch['retailer_key'] == 'arcteryx-official' else 'Retailer') + ' exact-variant quote; price excludes tax and shipping.',
    }


def check_watch(connection, watch_id, fetcher=None, now=None):
    return _check_watch(connection, watch_id, fetcher=fetcher, now=now)


def _check_watch(connection, watch_id, fetcher=None, now=None, due_before=None):
    instant = _now(now)
    stamp = canonical_time(instant)
    watch = _row(connection, watch_id)
    if not watch['enabled']:
        return _public(connection, watch, instant) | {'check_status': 'paused', 'alert': None, 'checks': []}
    token = uuid.uuid4().hex
    due_stamp = canonical_time(due_before) if due_before is not None else None
    with connection:
        acquired = connection.execute('''UPDATE monitor_watches SET lease_token=?,lease_until=?
            WHERE id=? AND enabled=1 AND (lease_until IS NULL OR lease_until<=?)
            AND (? IS NULL OR next_due_at IS NULL OR next_due_at<=?)''',
            (token, canonical_time(instant + timedelta(minutes=10)), watch_id, stamp,
             due_stamp, due_stamp)).rowcount
    if not acquired:
        current = _row(connection, watch_id)
        skip_status = 'not_due' if due_stamp and current['next_due_at'] and current['next_due_at'] > due_stamp else 'busy'
        return _public(connection, current, instant) | {'check_status': skip_status, 'alert': None, 'checks': []}
    watch = _row(connection, watch_id)
    checks = []
    alert = None
    try:
        fetcher = fetcher or _fetcher()
        first = _collect(connection, watch, fetcher, 'primary', instant)
        checks.append(first)
        status, error = first['status'], first['error']
        quote = first['quote']
        candidate = False
        event_threshold = watch['event_threshold_minor']
        if status == 'ok':
            eligible = quote['availability'] == 'in_stock' and quote['price_minor'] is not None
            meets_target = watch['target_price_minor'] is None or quote['price_minor'] is not None and quote['price_minor'] <= watch['target_price_minor']
            previous = metrics(connection, product_key=watch['product_key'], variant_key=watch['variant_key'],
                               market=watch['market'], currency=watch['currency'], as_of=quote['observed_at'],
                               retailer_key=watch['retailer_key'], offer_key=watch['offer_key'])['observed_all_time_min']
            if watch['rule'] == 'target':
                candidate = eligible and meets_target
                event_threshold = watch['target_price_minor']
            elif watch['rule'] == 'new_low':
                candidate = eligible and meets_target and previous is not None and quote['price_minor'] < previous['price_minor']
                if candidate:
                    event_threshold = previous['price_minor']
            else:
                candidate = eligible and meets_target and watch['last_explicit_availability'] == 'out_of_stock'
            candidate = bool(candidate and watch['armed'])
            if candidate:
                current = _row(connection, watch_id)
                if not current['enabled'] or current['config_version'] != watch['config_version']:
                    status, error = 'configuration_changed', 'Watch changed before confirmation; result discarded.'
                else:
                    confirmation = _collect(connection, watch, fetcher, 'confirmation', instant)
                    checks.append(confirmation)
                    if confirmation['status'] != 'ok':
                        status, error = 'verification_failed', confirmation['error']
                    elif any(confirmation['quote'][key] != quote[key] for key in ('price_minor', 'availability', 'source_url')):
                        status, error = 'verification_changed', 'Price or availability changed during confirmation; no alert emitted.'
                    else:
                        quote = confirmation['quote']
        finished = _now(now)
        finished_stamp = canonical_time(finished)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            current = _row(connection, watch_id)
            if current['lease_token'] != token:
                status, error = 'lease_lost', 'Another worker owns this check; result discarded.'
            elif current['config_version'] != watch['config_version'] or not current['enabled']:
                status, error = 'configuration_changed', 'Watch configuration changed during collection; result discarded.'
            if status in {'lease_lost', 'configuration_changed'}:
                if current['lease_token'] == token:
                    connection.execute('UPDATE monitor_watches SET lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?', (watch_id, token))
            else:
                updates = {'last_checked_at': finished_stamp, 'last_status': status, 'last_error': error,
                           'next_due_at': canonical_time(finished + timedelta(minutes=watch['interval_minutes'])),
                           'lease_token': None, 'lease_until': None}
                if status == 'ok':
                    if quote['price_minor'] is not None:
                        import_observations(_NoCommitConnection(connection), {'observations': [_observation(watch, quote)]})
                    updates |= {'last_success_at': finished_stamp, 'last_price_minor': quote['price_minor'],
                                'last_availability': quote['availability']}
                    if quote['availability'] != 'unknown':
                        updates['last_explicit_availability'] = quote['availability']
                    if candidate:
                        event = watch['event_number'] + 1
                        cursor = connection.execute('''INSERT INTO monitor_alerts
                            (watch_id,event_number,created_at,price_minor,currency,rule,url,label,evidence_sha256)
                            VALUES (?,?,?,?,?,?,?,?,?)''',
                            (watch_id,event,finished_stamp,quote['price_minor'],watch['currency'],watch['rule'],
                             watch['url'],watch['label'],quote['evidence_sha256']))
                        alert = dict(connection.execute('SELECT * FROM monitor_alerts WHERE id=?', (cursor.lastrowid,)).fetchone())
                        updates |= {'armed': 0, 'event_number': event, 'event_threshold_minor': event_threshold}
                    elif quote['availability'] == 'out_of_stock' or (
                        quote['availability'] == 'in_stock' and quote['price_minor'] is not None and
                        (watch['target_price_minor'] if watch['rule'] == 'target' else watch['event_threshold_minor']) is not None and
                        quote['price_minor'] > (watch['target_price_minor'] if watch['rule'] == 'target' else watch['event_threshold_minor'])
                    ):
                        updates['armed'] = 1
                connection.execute(f"UPDATE monitor_watches SET {','.join(f'{key}=?' for key in updates)} WHERE id=? AND lease_token=?",
                                   tuple(updates.values()) + (watch_id, token))
        return _public(connection, _row(connection, watch_id), finished) | {'check_status': status, 'alert': alert, 'checks': checks}
    except Exception as exc:
        # A source-identity conflict or database failure must invalidate the
        # displayed current quote, while preserving the previous success.
        finished = _now(now)
        with connection:
            connection.execute('''UPDATE monitor_watches SET last_checked_at=?,last_status=?,last_error=?,
                next_due_at=? WHERE id=? AND lease_token=?''',
                (canonical_time(finished), 'storage_error', str(exc)[:500],
                 canonical_time(finished + timedelta(minutes=watch['interval_minutes'])), watch_id, token))
        return _public(connection, _row(connection, watch_id), finished) | {
            'check_status': 'storage_error', 'alert': None, 'checks': checks}
    finally:
        with connection:
            connection.execute('UPDATE monitor_watches SET lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?', (watch_id, token))


def run_due(connection, fetcher=None, now=None, force=False):
    instant = _now(now)
    due = connection.execute('''SELECT id FROM monitor_watches
        WHERE enabled=1 AND (? OR next_due_at IS NULL OR next_due_at<=?) ORDER BY id''',
        (int(bool(force)), canonical_time(instant))).fetchall()
    results = [_check_watch(connection, row['id'], fetcher=fetcher, now=now,
                            due_before=None if force else instant) for row in due]
    return [result for result in results if result['check_status'] != 'not_due']
