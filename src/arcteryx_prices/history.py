"""Auditable historical references and conservative primary-snapshot backfills.

A publisher/date/price claim is useful evidence, but never itself proves an
exact variant was buyable. Imports remain separate from statistical observations.
Only a freshly fetched, exact timestamped primary retailer archive can be promoted.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .database import canonical_time, import_observations, parse_time

SCHEMA = Path(__file__).resolve().parents[2] / 'db' / 'history.sql'
REQUIRED = {
    'evidence_key', 'source_url', 'publisher', 'source_kind', 'observed_at',
    'date_precision', 'captured_at', 'price_minor', 'market', 'currency',
    'retailer_key', 'retailer_name', 'product_name', 'availability',
    'product_condition', 'price_type', 'excerpt', 'body_sha256',
}
OPTIONAL = {'model_code', 'sku', 'color', 'size', 'notes'}
# A host being accepted here only permits storing a cited reference. It is NOT
# sufficient to establish a price, condition, stock state or exact-variant match.
REPORT_HOSTS = {'slickdeals.net', 'www.slickdeals.net', 'forums.redflagdeals.com',
                'gearjunkie.com', 'www.switchbacktravel.com', 'www.outdoorgearlab.com'}
RETAILER_HOSTS = {'arcteryx.com', 'outlet.arcteryx.com', 'www.rei.com', 'rei.com',
                  'www.backcountry.com', 'backcountry.com', 'www.mec.ca', 'mec.ca',
                  'www.altitude-sports.com', 'altitude-sports.com'}


def initialize_history(connection):
    connection.executescript(SCHEMA.read_text(encoding='utf-8'))


def _url(value):
    if not isinstance(value, str) or len(value) > 4000 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('source_url must be an HTTPS URL')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('source_url must be HTTPS without credentials, custom port or fragment')
    return parsed


def _archive_identity(url):
    parsed = _url(url)
    if parsed.hostname != 'web.archive.org':
        raise ValueError('metric backfill requires a timestamped web.archive.org retailer snapshot')
    # id_ requests the unrewritten response; wildcard/latest and fuzzy dates are
    # not observation dates. Query parameters belong to the original product URL.
    match = re.fullmatch(r'/web/(\d{14})id_/(https://[^#]+)', parsed.path)
    if not match:
        raise ValueError('archive URL must include an exact 14-digit timestamp and id_')
    try:
        instant = datetime.strptime(match[1], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError('archive URL timestamp is invalid') from exc
    original = match[2] + ('?' + parsed.query if parsed.query else '')
    from .collector import validate_product_url
    location = validate_product_url(original)
    return canonical_time(instant), location


def _validate(record):
    if not isinstance(record, dict) or REQUIRED - record.keys() or record.keys() - REQUIRED - OPTIONAL:
        raise ValueError('historical evidence has missing or unknown fields')
    normalized = dict(record)
    for field in REQUIRED - {'price_minor'}:
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
        if len(record[field]) > (20000 if field == 'excerpt' else 4000):
            raise ValueError(f'{field} exceeds the length limit')
    for field in ('model_code', 'sku', 'color', 'size'):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 300):
            raise ValueError(f'{field} must be null or a nonempty string up to 300 characters')
        normalized[field] = value
    notes = record.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 20000:
        raise ValueError('notes must be a string up to 20000 characters')
    normalized['notes'] = notes
    if type(record['price_minor']) is not int or not 0 < record['price_minor'] <= 2**63 - 1:
        raise ValueError('price_minor must be positive integer cents')
    if (record['market'], record['currency']) not in {('US', 'USD'), ('CA', 'CAD')}:
        raise ValueError('supported market/currency pairs are US/USD and CA/CAD')
    for field, allowed in {
        'source_kind': {'deal_report', 'retailer_record', 'retailer_archive'},
        'date_precision': {'day', 'second'},
        'availability': {'in_stock', 'out_of_stock', 'unknown'},
        'product_condition': {'new', 'used', 'refurbished', 'unknown'},
        'price_type': {'public', 'coupon', 'member', 'bundle', 'unknown'},
    }.items():
        if record[field] not in allowed:
            raise ValueError(f'invalid {field}')
    normalized['captured_at'] = canonical_time(record['captured_at'])
    if record['date_precision'] == 'day':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', record['observed_at']):
            raise ValueError('day precision requires a YYYY-MM-DD price date')
        observed = date.fromisoformat(record['observed_at'])
        if observed > parse_time(record['captured_at']).date():
            raise ValueError('price date cannot follow capture date')
    else:
        normalized['observed_at'] = canonical_time(record['observed_at'])
        if normalized['observed_at'] > normalized['captured_at']:
            raise ValueError('price timestamp cannot follow capture timestamp')
    if parse_time(record['captured_at']) > datetime.now(timezone.utc):
        raise ValueError('capture timestamp cannot be in the future')
    if not re.fullmatch('[0-9a-f]{64}', record['body_sha256']):
        raise ValueError('body_sha256 must be a lowercase SHA-256 digest')
    host = _url(record['source_url']).hostname
    kind = record['source_kind']
    if kind == 'deal_report' and host not in REPORT_HOSTS:
        raise ValueError('report source host is not in the reviewed source registry')
    if kind == 'retailer_record' and host not in RETAILER_HOSTS:
        raise ValueError('retailer source host is not in the reviewed source registry')
    if kind == 'retailer_archive':
        timestamp, location = _archive_identity(record['source_url'])
        if record['date_precision'] != 'second' or normalized['observed_at'] != timestamp:
            raise ValueError('archive timestamp must match the exact price observation timestamp')
        if any(location[key] != record[key] for key in ('market', 'currency', 'retailer_key')):
            raise ValueError('archived retailer or market does not match the evidence')
    return normalized


def import_evidence(connection, payload):
    """Atomic/idempotent reference import; no self-declared trust flags accepted."""
    if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'evidence'}
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or not isinstance(payload['evidence'], list) or len(payload['evidence']) > 1000):
        raise ValueError('expected schema_version 1 and an evidence array of at most 1000 records')
    inserted = duplicates = 0
    with connection:
        for index, raw in enumerate(payload['evidence']):
            try:
                record = _validate(raw)
                old = connection.execute('SELECT * FROM historical_evidence WHERE evidence_key=? OR (source_url=? AND observed_at=?)',
                                         (record['evidence_key'], record['source_url'], record['observed_at'])).fetchone()
                if old:
                    previous = json.loads(old['payload_json'])
                    # A repeated retrieval does not rewrite the original audit date.
                    if {k: v for k, v in previous.items() if k != 'captured_at'} != {k: v for k, v in record.items() if k != 'captured_at'}:
                        raise ValueError('conflicting historical evidence; original record retained')
                    duplicates += 1
                    continue
                connection.execute('''INSERT INTO historical_evidence
                    (evidence_key,source_url,observed_at,captured_at,payload_json) VALUES (?,?,?,?,?)''',
                    (record['evidence_key'], record['source_url'], record['observed_at'], record['captured_at'],
                     json.dumps(record, sort_keys=True, ensure_ascii=False)))
                inserted += 1
            except (ValueError, sqlite3.IntegrityError) as exc:
                raise ValueError(f'evidence[{index}]: {exc}') from exc
    return {'inserted': inserted, 'duplicates': duplicates, 'metric_observations_inserted': 0}


def _exclusion(record):
    if record['source_kind'] == 'deal_report':
        return 'Secondary deal report; publication time is not a verified retailer observation and exact variant, stock or conditions may be unknown.'
    return 'Primary source reference has not passed exact-variant archive verification.'


def list_evidence(connection):
    result = []
    for row in connection.execute('SELECT * FROM historical_evidence ORDER BY observed_at DESC,id DESC'):
        record = json.loads(row['payload_json'])
        included = row['observation_id'] is not None
        result.append(record | {'included_in_metrics': included, 'watch_id': row['watch_id'],
                                'exclusion_reason': None if included else _exclusion(record),
                                'verification': json.loads(row['verification_json']) if row['verification_json'] else None})
    return result


class _ArchiveRedirect(HTTPRedirectHandler):
    def __init__(self, source_url):
        self.expected = _archive_identity(source_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if _archive_identity(target) != self.expected:
            raise ValueError('archive redirect changed snapshot date, source or variant URL')
        return super().redirect_request(req, fp, code, msg, headers, target)


def _read_archive(source_url):
    from .collector import MAX_RESPONSE_BYTES, REQUEST_TIMEOUT_SECONDS
    expected = _archive_identity(source_url)
    opener = build_opener(_ArchiveRedirect(source_url))
    request = Request(source_url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ArcPriceMonitor/0.1)',
                                          'Accept': 'text/html', 'Accept-Encoding': 'identity'})
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 200 or _archive_identity(response.geturl()) != expected:
                raise ValueError('archive response did not preserve the requested snapshot')
            if response.headers.get_content_type() not in {'text/html', 'application/xhtml+xml'}:
                raise ValueError('archive response must be HTML')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('archive response must use identity encoding')
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError('archive response exceeds size limit')
            return body
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise ValueError(f'archive fetch failed ({type(exc).__name__}); evidence remains reference-only') from exc


def backfill_evidence(connection, evidence_key, watch_id):
    """Verify archived public retailer HTML before adding a historical data point.

    This operation never writes watch freshness, schedules, check history or alerts.
    Imported references cannot promote themselves via a trust/included flag.
    """
    from .collector import parse_product_html, validate_product_url
    from .monitor import _NoCommitConnection, _observation, _row
    row = connection.execute('SELECT * FROM historical_evidence WHERE evidence_key=?', (evidence_key,)).fetchone()
    if row is None:
        raise ValueError('historical evidence not found')
    evidence = json.loads(row['payload_json'])
    watch = _row(connection, watch_id)
    if evidence['source_kind'] != 'retailer_archive':
        raise ValueError('only a primary retailer archive can enter exact-variant metrics')
    timestamp, location = _archive_identity(evidence['source_url'])
    if location['url'] != validate_product_url(watch['url'])['url']:
        raise ValueError('archived original URL does not match the watched product URL')
    for field in ('market', 'currency', 'retailer_key', 'sku', 'color', 'size'):
        if evidence[field] != watch[field]:
            raise ValueError(f'historical {field} does not exactly match the watch')
    if (evidence['availability'], evidence['product_condition'], evidence['price_type']) != ('in_stock', 'new', 'public'):
        raise ValueError('backfill requires an in-stock new public-price offer')
    if row['observation_id'] is not None:
        if row['watch_id'] != watch_id:
            raise ValueError('historical evidence is already linked to another watch')
        return {'inserted': 0, 'duplicates': 1, 'evidence_key': evidence_key, 'watch_id': watch_id}
    body = _read_archive(evidence['source_url'])
    digest = hashlib.sha256(body).hexdigest()
    if digest != evidence['body_sha256']:
        raise ValueError('archive content digest changed; review the new evidence before backfilling')
    quote = parse_product_html(body, watch, observed_at=timestamp)
    if quote['price_minor'] != evidence['price_minor'] or quote['availability'] != 'in_stock':
        raise ValueError('archived exact-variant price or stock does not match the evidence')
    captured = canonical_time(datetime.now(timezone.utc))
    record = _observation(watch, quote | {'observed_at': timestamp, 'captured_at': captured,
                                        'source_url': evidence['source_url']})
    record |= {'source_type': 'archive',
               'notes': f'Verified exact-variant archived retailer HTML; evidence={evidence_key}; SHA-256={digest}; excludes tax/shipping.'}
    verification = {'verified_at': captured, 'body_sha256': digest, 'original_url': location['url'],
                    'method': 'timestamped primary retailer HTML parsed for exact SKU/color/size/market/public/new/in-stock offer'}
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        imported = import_observations(_NoCommitConnection(connection), {'observations': [record]})
        observation = connection.execute('''SELECT po.id FROM price_observations po
            JOIN offers o ON o.id=po.offer_id JOIN retailers r ON r.id=o.retailer_id
            WHERE o.offer_key=? AND r.retailer_key=? AND po.observed_at=? AND po.source_url=?''',
            (watch['offer_key'], watch['retailer_key'], timestamp, evidence['source_url'])).fetchone()
        connection.execute('UPDATE historical_evidence SET observation_id=?,watch_id=?,verification_json=? WHERE id=?',
                           (observation['id'], watch_id, json.dumps(verification, sort_keys=True), row['id']))
    return imported | {'evidence_key': evidence_key, 'watch_id': watch_id}
