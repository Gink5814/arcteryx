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
from urllib.parse import urlsplit

from .database import canonical_time, import_observations, initialize, metrics, parse_time
from .scope import MARKET, CURRENCY, require_scope


MONITOR_SCHEMA = Path(__file__).resolve().parents[2] / 'db' / 'monitor.sql'
UTC = timezone.utc
MAX_RETRY_DELAY = timedelta(hours=24)
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL_MINUTES = 60


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
    from .collector import BatchQuoteCollector
    return BatchQuoteCollector()


def initialize_monitor(db_path):
    connection = initialize(db_path)
    connection.execute('PRAGMA busy_timeout = 10000')
    connection.executescript(MONITOR_SCHEMA.read_text(encoding='utf-8'))
    # Additive migration preserves existing official watch and price identities.
    migrations = {
        'monitor_watches': (
            ('retailer_key', "TEXT NOT NULL DEFAULT 'arcteryx-official'"),
            ('retailer_name', "TEXT NOT NULL DEFAULT 'Arc''teryx official'"),
            ('consecutive_failures', 'INTEGER NOT NULL DEFAULT 0'),
            ('retry_not_before', 'TEXT'),
        ),
        'monitor_checks': (('http_status', 'INTEGER'), ('retryable', 'INTEGER NOT NULL DEFAULT 0'),
                           ('retry_after', 'TEXT')),
    }
    for table, additions in migrations.items():
        columns = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
        for name, declaration in additions:
            if name not in columns:
                try:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
                except sqlite3.OperationalError:
                    if name not in {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}:
                        raise
    from .history import initialize_history
    initialize_history(connection)
    from .migrations import migrate_watch_interval
    try:
        migrate_watch_interval(connection)
    except Exception:
        connection.close()
        raise
    _cap_saved_retries(connection)
    # The product now supports US/USD only. Preserve old quotes and identities,
    # but retire other watches and invalidate any old worker's lease atomically.
    with connection:
        connection.execute('''UPDATE monitor_watches SET enabled=0,
            lease_token=NULL, lease_until=NULL, config_version=config_version+1,
            updated_at=? WHERE (market<>? OR currency<>?)
            AND (enabled<>0 OR lease_token IS NOT NULL OR lease_until IS NOT NULL)''',
            (canonical_time(_now()), MARKET, CURRENCY))
    return connection


def _cap_saved_retries(connection):
    """Repair old unbounded waits relative to the recorded failure, not startup."""
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        for row in connection.execute('SELECT host,not_before,updated_at FROM monitor_host_cooldowns'):
            ceiling = parse_time(row['updated_at']) + MAX_RETRY_DELAY
            if parse_time(row['not_before']) > ceiling:
                connection.execute('UPDATE monitor_host_cooldowns SET not_before=? WHERE host=?',
                                   (canonical_time(ceiling), row['host']))
        for row in connection.execute('''SELECT id,last_checked_at,retry_not_before,next_due_at
                FROM monitor_watches WHERE retry_not_before IS NOT NULL AND last_checked_at IS NOT NULL'''):
            ceiling = parse_time(row['last_checked_at']) + MAX_RETRY_DELAY
            for field in ('retry_not_before', 'next_due_at'):
                if row[field] and parse_time(row[field]) > ceiling:
                    connection.execute(f'UPDATE monitor_watches SET {field}=? WHERE id=?',
                                       (canonical_time(ceiling), row['id']))


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
        interval = payload.get('interval_minutes', DEFAULT_INTERVAL_MINUTES)
        if type(interval) is not int or not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
            raise ValueError('interval_minutes must be an integer from 15 to 1440')
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


def _cooldown_until(connection, url, instant):
    row = connection.execute('SELECT not_before FROM monitor_host_cooldowns WHERE host=?',
                             (urlsplit(url).hostname,)).fetchone()
    return row['not_before'] if row and parse_time(row['not_before']) > instant else None


def _waiting(connection, watch, instant):
    host_until = _cooldown_until(connection, watch['url'], instant)
    retry_until = watch.get('retry_not_before')
    if retry_until and parse_time(retry_until) <= instant:
        retry_until = None
    deadlines = [value for value in (host_until, retry_until) if value]
    return (max(deadlines) if deadlines else None), host_until


def _failure_metadata(exc, status=None, now=None):
    code = getattr(exc, 'http_status', None)
    if type(code) is not int or not 100 <= code <= 599:
        code = None
    status = status or getattr(exc, 'status', None)
    retryable = (status == 'network_error' or code in (408, 429)
                 or code is not None and 500 <= code <= 599)
    retry_after = getattr(exc, 'retry_after', None)
    try:
        retry_after = (canonical_time(min(parse_time(canonical_time(retry_after)),
                                         _now(now) + MAX_RETRY_DELAY))
                       if retry_after is not None else None)
    except (TypeError, ValueError, AttributeError):
        retry_after = None
    response_url = getattr(exc, 'response_url', None)
    if response_url is not None:
        try:
            response_url = _validate_url(response_url)['url']
        except (TypeError, ValueError):
            response_url = None
    return {'http_status': code, 'retryable': bool(retryable), 'retry_after': retry_after,
            'response_url': response_url}


def _failure_delay(interval_minutes, failures):
    # First failure uses the normal interval, then doubles to at most 24 hours.
    return timedelta(minutes=min(1440, interval_minutes * 2 ** min(max(failures - 1, 0), 16)))


def _record_host_failure(connection, url, metadata, instant, fallback):
    code, requested = metadata['http_status'], metadata['retry_after']
    if code != 429 and not (code == 503 and requested):
        return
    deadline = max(instant + fallback, parse_time(requested)) if requested else instant + fallback
    ceiling = instant + MAX_RETRY_DELAY
    deadline = min(deadline, ceiling)
    hosts = {urlsplit(url).hostname}
    if metadata.get('response_url'):
        hosts.add(urlsplit(metadata['response_url']).hostname)
    stamp = canonical_time(instant)
    with connection:
        for host in hosts:
            connection.execute('''INSERT INTO monitor_host_cooldowns(host,not_before,http_status,updated_at)
                VALUES (?,?,?,?) ON CONFLICT(host) DO UPDATE SET
                not_before=MIN(MAX(monitor_host_cooldowns.not_before,excluded.not_before),?),
                http_status=excluded.http_status,updated_at=excluded.updated_at''',
                (host, canonical_time(deadline), code, stamp, canonical_time(ceiling)))


def discover_for_monitor(connection, url, now=None):
    """Discovery obeys the same persisted source cooldown as scheduled checks."""
    from .collector import CollectorError, discover_variants, validate_product_url
    location = validate_product_url(url)
    instant = _now(now)
    deadline = _cooldown_until(connection, location['url'], instant)
    if deadline:
        raise CollectorError('rate_limited', 'This source is waiting before its next request.',
                             retryable=True, retry_after=parse_time(deadline))
    try:
        return discover_variants(location['url'])
    except CollectorError as exc:
        failed_at = _now(now)
        metadata = _failure_metadata(exc, now=failed_at)
        exc.retry_after = parse_time(metadata['retry_after']) if metadata['retry_after'] else None
        _record_host_failure(connection, location['url'], metadata, failed_at, timedelta(minutes=60))
        deadline = _cooldown_until(connection, location['url'], failed_at)
        if deadline:
            # The local policy can wait longer than Retry-After. Tell callers
            # the actual persisted deadline, including on the first failure.
            exc.retry_after = parse_time(deadline)
        raise


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
    result['next_retry_at'], result['cooldown_until'] = _waiting(connection, row, instant)
    result['metrics'] = metrics(
        connection, product_key=row['product_key'], variant_key=row['variant_key'],
        market=row['market'], currency=row['currency'], as_of=canonical_time(instant),
        retailer_key=row['retailer_key'], offer_key=row['offer_key'],
    )
    return result


def _watch_values(payload):
    if not isinstance(payload, dict):
        raise ValueError('watch must be an object')
    allowed = {'url', 'sku', 'color', 'size', 'label', 'target_price_minor', 'rule', 'interval_minutes'}
    if set(payload) - allowed:
        raise ValueError(f'unknown watch fields: {sorted(set(payload)-allowed)}')
    url = _validate_url(_text(payload.get('url'), 'url', 2000))
    require_scope(url)
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
    return values


def _insert_watch(connection, values):
    return connection.execute(
        f"INSERT INTO monitor_watches ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    ).lastrowid


def create_watch(connection, payload):
    values = _watch_values(payload)
    try:
        with connection:
            watch_id = _insert_watch(connection, values)
    except sqlite3.IntegrityError as exc:
        raise ValueError('this retailer URL and SKU are already watched') from exc
    return _public(connection, _row(connection, watch_id))


def import_watches(connection, payload):
    """Atomically add a portable watch list without replacing existing settings."""
    if not isinstance(payload, list):
        raise ValueError('watch import must be an array of watch objects')
    result = {'created': 0, 'skipped': 0, 'watch_ids': []}
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        for index, item in enumerate(payload):
            try:
                values = _watch_values(item)
                existing = connection.execute('SELECT * FROM monitor_watches WHERE url=? AND sku=?',
                                              (values['url'], values['sku'])).fetchone()
                if existing:
                    if any(existing[key] != values[key] for key in ('color', 'size', 'market', 'currency')):
                        raise ValueError('existing URL and SKU have a different variant identity')
                    watch_id = existing['id']
                    result['skipped'] += 1
                else:
                    watch_id = _insert_watch(connection, values)
                    result['created'] += 1
                result['watch_ids'].append(watch_id)
            except ValueError as exc:
                raise ValueError(f'watch item {index + 1}: {exc}') from exc
    return result


def list_watches(connection, now=None):
    return [_public(connection, dict(row), now) for row in connection.execute(
        'SELECT * FROM monitor_watches WHERE market=? AND currency=? ORDER BY id', (MARKET, CURRENCY))]


def update_watch(connection, watch_id, payload):
    if not isinstance(payload, dict) or set(payload) - {'enabled', 'target_price_minor', 'rule', 'interval_minutes'}:
        raise ValueError('only enabled, target_price_minor, rule, and interval_minutes can be updated')
    settings = _settings(payload)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        old = _row(connection, watch_id)
        require_scope(old)
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
    return [dict(row) for row in connection.execute('''SELECT a.*,w.market AS market FROM monitor_alerts a
        JOIN monitor_watches w ON w.id=a.watch_id
        WHERE w.market=? AND w.currency=? AND a.currency=? ORDER BY a.id DESC''',
        (MARKET, CURRENCY, CURRENCY))]


def _page_identity(location):
    """Identify an already validated product page without tracking parameters."""
    parsed = urlsplit(location['url'])
    return parsed.scheme, parsed.netloc, parsed.path, location['market'], location['currency']


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
    watched_location = _validate_url(watch['url'])
    if (any(location[key] != watch[key] for key in ('market', 'currency'))
            or _page_identity(location) != _page_identity(watched_location)):
        raise ValueError('quote source URL or market does not match the watch')
    result['source_url'] = location['url']
    for key in ('sku', 'color', 'size', 'market', 'currency'):
        if key in value and value[key] != watch[key]:
            raise ValueError(f'quote {key} does not match the watched variant')
    return result


def _collect(connection, watch, fetcher, stage, instant, now=None):
    from .collector import BatchQuoteCollector
    deadline = _cooldown_until(connection, watch['url'], _now(now))
    if deadline:
        return {'stage': stage, 'status': 'rate_limited', 'error': 'Source cooldown is active.',
                'quote': None, 'http_status': None, 'retryable': True,
                'retry_after': deadline, 'attempted': False}
    try:
        value = (fetcher.collect(dict(watch), stage=stage) if isinstance(fetcher, BatchQuoteCollector)
                 else fetcher(dict(watch)))
        quote = _quote(watch, value)
        entry = {'stage': stage, 'status': 'ok', 'error': None, 'quote': quote,
                 'http_status': None, 'retryable': False, 'retry_after': None, 'attempted': True}
    except Exception as exc:
        status = getattr(exc, 'status', None)
        if not isinstance(status, str) or not re.fullmatch('[a-z_]{1,64}', status):
            status = 'parse_error' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'network_error'
        failed_at = _now(now)
        entry = {'stage': stage, 'status': status, 'error': str(exc)[:500], 'quote': None,
                 'attempted': True} | _failure_metadata(exc, status, now=failed_at)
        _record_host_failure(connection, watch['url'], entry, failed_at,
                             _failure_delay(watch['interval_minutes'], watch['consecutive_failures'] + 1))
    with connection:
        connection.execute('''INSERT INTO monitor_checks
            (watch_id,checked_at,stage,status,error,quote_json,http_status,retryable,retry_after)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (watch['id'], canonical_time(instant), stage, entry['status'], entry['error'],
             json.dumps(entry['quote'], sort_keys=True) if entry['quote'] else None,
             entry['http_status'], int(entry['retryable']), entry['retry_after']))
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
    require_scope(watch)
    if not watch['enabled']:
        return _public(connection, watch, instant) | {'check_status': 'paused', 'alert': None, 'checks': []}
    deadline, host_deadline = _waiting(connection, watch, instant)
    if deadline:
        return _public(connection, watch, instant) | {
            'check_status': 'rate_limited' if host_deadline else 'backoff', 'alert': None, 'checks': []}
    token = uuid.uuid4().hex
    due_stamp = canonical_time(due_before) if due_before is not None else None
    with connection:
        acquired = connection.execute('''UPDATE monitor_watches SET lease_token=?,lease_until=?
            WHERE id=? AND enabled=1 AND market=? AND currency=? AND (lease_until IS NULL OR lease_until<=?)
            AND (retry_not_before IS NULL OR retry_not_before<=?)
            AND NOT EXISTS(SELECT 1 FROM monitor_host_cooldowns WHERE host=? AND not_before>?)
            AND (? IS NULL OR next_due_at IS NULL OR next_due_at<=?)''',
            (token, canonical_time(instant + timedelta(minutes=10)), watch_id, MARKET, CURRENCY, stamp,
             stamp, urlsplit(watch['url']).hostname, stamp,
             due_stamp, due_stamp)).rowcount
    if not acquired:
        current = _row(connection, watch_id)
        waiting, host_deadline = _waiting(connection, current, instant)
        skip_status = ('rate_limited' if host_deadline else 'backoff') if waiting else (
            'not_due' if due_stamp and current['next_due_at'] and current['next_due_at'] > due_stamp else 'busy')
        return _public(connection, current, instant) | {'check_status': skip_status, 'alert': None, 'checks': []}
    watch = _row(connection, watch_id)
    checks = []
    alert = None
    try:
        fetcher = fetcher or _fetcher()
        first = _collect(connection, watch, fetcher, 'primary', instant, now=now)
        if not first['attempted']:
            return _public(connection, _row(connection, watch_id), _now(now)) | {
                'check_status': 'rate_limited', 'alert': None, 'checks': [], 'checking': False}
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
                    confirmation = _collect(connection, watch, fetcher, 'confirmation', instant, now=now)
                    if not confirmation['attempted']:
                        return _public(connection, _row(connection, watch_id), _now(now)) | {
                            'check_status': 'rate_limited', 'alert': None, 'checks': checks, 'checking': False}
                    checks.append(confirmation)
                    if confirmation['status'] != 'ok':
                        status = 'site_blocked' if confirmation['status'] == 'site_blocked' else 'verification_failed'
                        error = confirmation['error']
                    # _quote already validated both source pages and exact
                    # variants. Tracking query changes do not change identity.
                    elif any(confirmation['quote'][key] != quote[key] for key in ('price_minor', 'availability')):
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
                           'lease_token': None, 'lease_until': None,
                           'retry_not_before': None}
                if status == 'ok':
                    updates['consecutive_failures'] = 0
                    if quote['price_minor'] is not None:
                        import_observations(_NoCommitConnection(connection), {'observations': [_observation(watch, quote)]})
                    # Cached primary pages retain their actual retrieval time;
                    # parsing another SKU must not extend snapshot freshness.
                    updates |= {'last_success_at': quote['captured_at'], 'last_price_minor': quote['price_minor'],
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
                else:
                    failures = current['consecutive_failures'] + 1
                    updates['consecutive_failures'] = failures
                    failed = checks[-1]
                    if failed['retryable']:
                        retry_at = finished + _failure_delay(watch['interval_minutes'], failures)
                        if failed['retry_after']:
                            retry_at = max(retry_at, parse_time(failed['retry_after']))
                        host_until = _cooldown_until(connection, watch['url'], finished)
                        if host_until:
                            retry_at = max(retry_at, parse_time(host_until))
                        updates['retry_not_before'] = canonical_time(min(retry_at, finished + MAX_RETRY_DELAY))
                        updates['next_due_at'] = updates['retry_not_before']
                connection.execute(f"UPDATE monitor_watches SET {','.join(f'{key}=?' for key in updates)} WHERE id=? AND lease_token=?",
                                   tuple(updates.values()) + (watch_id, token))
        return _public(connection, _row(connection, watch_id), finished) | {'check_status': status, 'alert': alert, 'checks': checks}
    except Exception as exc:
        # A source-identity conflict or database failure must invalidate the
        # displayed current quote, while preserving the previous success.
        finished = _now(now)
        with connection:
            connection.execute('''UPDATE monitor_watches SET last_checked_at=?,last_status=?,last_error=?,
                next_due_at=?,consecutive_failures=consecutive_failures+1,retry_not_before=NULL
                WHERE id=? AND lease_token=?''',
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
        WHERE enabled=1 AND market=? AND currency=? AND (? OR next_due_at IS NULL OR next_due_at<=?) ORDER BY id''',
        (MARKET, CURRENCY, int(bool(force)), canonical_time(instant))).fetchall()
    # One cycle shares primary page snapshots; each confirmation bypasses them.
    fetcher = fetcher if fetcher is not None else _fetcher()
    results = [_check_watch(connection, row['id'], fetcher=fetcher, now=now,
                            due_before=None if force else instant) for row in due]
    return [result for result in results if result['check_status'] != 'not_due']
