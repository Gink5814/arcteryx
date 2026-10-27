"""Historical source tests use isolated databases and SYNTHETIC archive bytes only."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from arcteryx_prices.collector import CollectorError
from arcteryx_prices.database import canonical_time
from arcteryx_prices.history import (
    _ArchiveRedirect, _archive_identity, backfill_evidence, import_evidence, list_evidence,
)
from arcteryx_prices.monitor import check_watch, create_watch, initialize_monitor, list_watches

URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
ARCHIVE = 'https://web.archive.org/web/20250901120000id_/' + URL
BODY = (Path(__file__).parent / 'fixtures' / 'collector_synthetic.html').read_bytes()


def reference(**changes):
    return {
        'evidence_key': 'SYNTHETIC-REPORT-1',
        'source_url': 'https://slickdeals.net/f/123-synthetic-test-only',
        'publisher': 'SYNTHETIC Slickdeals fixture', 'source_kind': 'deal_report',
        'observed_at': '2025-09-01', 'date_precision': 'day',
        'captured_at': '2026-01-01T12:00:00Z', 'price_minor': 49995,
        'market': 'US', 'currency': 'USD', 'retailer_key': 'arcteryx-official',
        'retailer_name': "Arc'teryx official", 'product_name': 'SYNTHETIC shell',
        'availability': 'unknown', 'product_condition': 'unknown', 'price_type': 'public',
        'excerpt': 'SYNTHETIC fixture, not an actual historical retail price.',
        'body_sha256': hashlib.sha256(BODY).hexdigest(),
    } | changes


def archive(**changes):
    return reference(
        evidence_key='SYNTHETIC-ARCHIVE-1', source_url=ARCHIVE,
        publisher='SYNTHETIC archived retailer fixture', source_kind='retailer_archive',
        observed_at='2025-09-01T12:00:00Z', date_precision='second',
        availability='in_stock', product_condition='new', sku='SYNTHETIC-BLACK-M',
        color='Black', size='M',
    ) | changes


def payload(*records):
    return {'schema_version': 1, 'evidence': list(records)}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.db = initialize_monitor(self.path)
        self.watch = create_watch(self.db, {
            'url': URL, 'sku': 'SYNTHETIC-BLACK-M', 'color': 'Black', 'size': 'M',
            'rule': 'new_low', 'label': 'SYNTHETIC history watch',
        })

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def count(self, table):
        return self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def test_secondary_report_keeps_date_precision_and_never_changes_metrics(self):
        before = dict(self.db.execute('SELECT * FROM monitor_watches').fetchone())
        self.assertEqual(import_evidence(self.db, payload(reference()))['inserted'], 1)
        evidence = list_evidence(self.db)[0]
        self.assertEqual(evidence['observed_at'], '2025-09-01')
        self.assertFalse(evidence['included_in_metrics'])
        self.assertIn('Secondary', evidence['exclusion_reason'])
        self.assertIsNone(evidence['watch_id'])
        self.assertIsNone(list_watches(self.db)[0]['metrics']['observed_all_time_min'])
        self.assertEqual(before, dict(self.db.execute('SELECT * FROM monitor_watches').fetchone()))
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(self.count('monitor_alerts'), 0)
        with patch('arcteryx_prices.history._read_archive') as fetch:
            with self.assertRaisesRegex(ValueError, 'only a primary'):
                backfill_evidence(self.db, 'SYNTHETIC-REPORT-1', self.watch['id'])
            fetch.assert_not_called()

    def test_idempotent_import_keeps_original_capture_and_conflicts_roll_back_batch(self):
        record = reference()
        import_evidence(self.db, payload(record))
        retry = record | {'captured_at': '2026-02-01T12:00:00Z'}
        self.assertEqual(import_evidence(self.db, payload(retry))['duplicates'], 1)
        self.assertEqual(list_evidence(self.db)[0]['captured_at'], canonical_time(record['captured_at']))
        first = reference(evidence_key='SECOND', source_url='https://slickdeals.net/f/456-synthetic')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            import_evidence(self.db, payload(first, record | {'price_minor': 1}))
        self.assertEqual(self.count('historical_evidence'), 1)
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            import_evidence(self.db, payload(record | {'evidence_key': 'DUPLICATE-URL'}))

    def test_source_registry_currency_dates_digest_and_trust_flags_fail_closed(self):
        for changes in (
            {'currency': 'CAD'}, {'market': 'GB'}, {'price_minor': True}, {'price_minor': 0},
            {'date_precision': 'day', 'observed_at': '2025-09-01T12:00:00Z'},
            {'date_precision': 'second', 'observed_at': '2025-09-01T12:00:00'},
            {'observed_at': '2027-01-01'}, {'captured_at': '2999-01-01T00:00:00Z'},
            {'body_sha256': 'not-a-digest'}, {'source_url': 'https://evil.invalid/history'},
            {'source_url': 'http://slickdeals.net/f/123'},
            {'source_url': 'https://user@www.slickdeals.net/f/123'},
            {'source_url': 'https://slickdeals.net:8443/f/123'},
            {'source_url': 'https://slickdeals.net/f/123\x00'},
            {'source_url': 'https://slickdeals.net/f/123\n'},
            {'source_url': 'https://slickdeals.net.evil.invalid/f/123'},
            {'included_in_metrics': True}, {'trusted': True}, {'source_kind': 'verified'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                import_evidence(self.db, payload(reference(**changes)))
        self.assertEqual(self.count('historical_evidence'), 0)
        self.assertEqual(self.count('price_observations'), 0)

    def test_exact_archive_backfill_adds_one_sparse_point_without_current_quote_or_alert(self):
        record = archive()
        import_evidence(self.db, payload(record))
        before = dict(self.db.execute('SELECT * FROM monitor_watches').fetchone())
        with patch('arcteryx_prices.history._read_archive', return_value=BODY) as fetch:
            result = backfill_evidence(self.db, record['evidence_key'], self.watch['id'])
            self.assertEqual(result['inserted'], 1)
            retry = backfill_evidence(self.db, record['evidence_key'], self.watch['id'])
            self.assertEqual(retry['duplicates'], 1)
            fetch.assert_called_once_with(ARCHIVE)
        watch = list_watches(self.db, now='2025-09-02T12:00:00Z')[0]
        self.assertIsNone(watch['current_price_minor'])
        self.assertEqual(watch['status'], 'never_checked')
        self.assertEqual(watch['metrics']['observed_all_time_min']['price_minor'], 49995)
        self.assertEqual(watch['metrics']['average_daily_low_90d']['observed_days'], 1)
        self.assertEqual(watch['metrics']['average_daily_low_90d']['missing_days'], 89)
        self.assertEqual(watch['metrics']['observed_all_time_min']['source_type'], 'archive')
        evidence = list_evidence(self.db)[0]
        self.assertTrue(evidence['included_in_metrics'])
        self.assertEqual(evidence['watch_id'], watch['id'])
        self.assertEqual(evidence['verification']['body_sha256'], hashlib.sha256(BODY).hexdigest())
        self.assertEqual(before, dict(self.db.execute('SELECT * FROM monitor_watches').fetchone()))
        self.assertEqual(self.count('monitor_checks'), 0)
        self.assertEqual(self.count('monitor_alerts'), 0)

    def test_backfill_preserves_a_live_current_quote_and_existing_schedule(self):
        now = datetime.now(timezone.utc).isoformat()
        quote = {'price_minor': 65000, 'availability': 'in_stock', 'observed_at': now,
                 'captured_at': now, 'source_url': URL, 'evidence_sha256': 'a' * 64}
        checked = check_watch(self.db, self.watch['id'], fetcher=lambda _: quote)
        self.assertEqual(checked['current_price_minor'], 65000)
        before = dict(self.db.execute('SELECT * FROM monitor_watches').fetchone())
        import_evidence(self.db, payload(archive()))
        with patch('arcteryx_prices.history._read_archive', return_value=BODY):
            backfill_evidence(self.db, 'SYNTHETIC-ARCHIVE-1', self.watch['id'])
        current = list_watches(self.db)[0]
        self.assertEqual(current['current_price_minor'], 65000)
        self.assertEqual(current['metrics']['observed_all_time_min']['price_minor'], 49995)
        self.assertEqual(before, dict(self.db.execute('SELECT * FROM monitor_watches').fetchone()))
        self.assertEqual(self.count('monitor_checks'), 1)
        self.assertEqual(self.count('monitor_alerts'), 0)

    def test_archive_uses_historical_offer_validity_not_current_date(self):
        body = BODY.replace(b'"price": "499.95"', b'"price": "499.95", "priceValidUntil": "2025-09-02"')
        record = archive(body_sha256=hashlib.sha256(body).hexdigest())
        import_evidence(self.db, payload(record))
        with patch('arcteryx_prices.history._read_archive', return_value=body):
            result = backfill_evidence(self.db, record['evidence_key'], self.watch['id'])
        self.assertEqual(result['inserted'], 1)

    def test_archive_wrong_exact_identity_or_conditions_never_fetch_or_promote(self):
        for index, changes in enumerate(({'sku': None}, {'sku': 'WRONG'}, {'size': 'L'},
                                        {'color': 'Blue'}, {'availability': 'unknown'},
                                        {'product_condition': 'used'}, {'price_type': 'coupon'})):
            record = archive(**changes, evidence_key=f'SYNTHETIC-{index}')
            with self.subTest(changes=changes):
                import_evidence(self.db, payload(record))
                with patch('arcteryx_prices.history._read_archive') as fetch:
                    with self.assertRaises(ValueError):
                        backfill_evidence(self.db, record['evidence_key'], self.watch['id'])
                    fetch.assert_not_called()
                self.db.execute('DELETE FROM historical_evidence')
                self.db.commit()
        self.assertEqual(self.count('price_observations'), 0)

    def test_changed_archive_hash_price_stock_currency_and_variant_fail_before_metric_write(self):
        for index, (body, changes) in enumerate((
            (BODY + b'changed', {}),
            (BODY, {'price_minor': 100}),
            (BODY.replace(b'InStock', b'OutOfStock'), {}),
            (BODY.replace(b'USD', b'CAD'), {}),
            (BODY.replace(b'SYNTHETIC-BLACK-M', b'WRONG'), {}),
            (BODY.replace(b'"size": "M"', b'"size": "XS"'), {}),
        )):
            changes = dict(changes)
            if index:
                changes['body_sha256'] = hashlib.sha256(body).hexdigest()
            record = archive(**changes)
            with self.subTest(index=index):
                import_evidence(self.db, payload(record))
                with patch('arcteryx_prices.history._read_archive', return_value=body):
                    with self.assertRaises((ValueError, CollectorError)):
                        backfill_evidence(self.db, record['evidence_key'], self.watch['id'])
                self.assertEqual(self.count('price_observations'), 0)
                self.assertFalse(list_evidence(self.db)[0]['included_in_metrics'])
                self.db.execute('DELETE FROM historical_evidence')
                self.db.commit()

    def test_archive_time_market_url_and_redirect_are_bound_to_source(self):
        for changes in (
            {'observed_at': '2025-09-01T13:00:00Z'},
            {'source_url': ARCHIVE.replace('20250901120000', '202509')},
            {'source_url': ARCHIVE.replace('20250901120000', '*')},
            {'source_url': ARCHIVE.replace('id_/', '/')},
            {'source_url': ARCHIVE.replace('https://arcteryx.com', 'https://127.0.0.1')},
            {'source_url': ARCHIVE.replace('/us/en/', '/ca/en/')},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, CollectorError)):
                import_evidence(self.db, payload(archive(**changes)))
        handler = _ArchiveRedirect(ARCHIVE)
        for target in (ARCHIVE.replace('20250901120000', '20250902120000'),
                       ARCHIVE.replace('synthetic-shell', 'another-shell'),
                       ARCHIVE.replace('/us/en/', '/ca/en/'), 'https://evil.invalid/'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(Request(ARCHIVE), None, 302, 'Found', {}, target)

    def test_evidence_link_failure_rolls_back_historical_observation(self):
        record = archive()
        import_evidence(self.db, payload(record))
        self.db.executescript("""CREATE TRIGGER synthetic_link_failure BEFORE UPDATE ON historical_evidence
            BEGIN SELECT RAISE(ABORT, 'synthetic evidence link failure'); END;""")
        with patch('arcteryx_prices.history._read_archive', return_value=BODY):
            with self.assertRaises(sqlite3.IntegrityError):
                backfill_evidence(self.db, record['evidence_key'], self.watch['id'])
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(self.count('products'), 0)
        self.assertFalse(list_evidence(self.db)[0]['included_in_metrics'])

    def test_additive_retailer_migration_preserves_existing_official_records(self):
        before = dict(self.db.execute('SELECT * FROM monitor_watches').fetchone())
        self.db.execute('ALTER TABLE monitor_watches DROP COLUMN retailer_key')
        self.db.execute('ALTER TABLE monitor_watches DROP COLUMN retailer_name')
        self.db.close()
        self.db = initialize_monitor(self.path)
        after = dict(self.db.execute('SELECT * FROM monitor_watches').fetchone())
        self.assertEqual(before, after)
        self.assertTrue(after['product_key'].startswith('official:'))

    def test_third_party_watch_identity_is_not_an_official_observation(self):
        url = 'https://www.rei.com/product/123456/arcteryx-synthetic-shell'
        watch = create_watch(self.db, {
            'url': url, 'sku': 'SYNTHETIC-BLACK-M', 'color': 'Black', 'size': 'M',
            'rule': 'new_low', 'label': 'SYNTHETIC REI watch',
        })
        self.assertEqual(watch['retailer_key'], 'rei')
        self.assertEqual(watch['retailer_name'], 'REI Co-op')
        self.assertTrue(watch['product_key'].startswith('rei:'))
        now = datetime.now(timezone.utc).isoformat()
        quote = {'price_minor': 50000, 'availability': 'in_stock', 'observed_at': now,
                 'captured_at': now, 'source_url': url, 'evidence_sha256': 'a' * 64}
        checked = check_watch(self.db, watch['id'], fetcher=lambda _: quote)
        self.assertEqual(checked['check_status'], 'ok')
        self.assertEqual(checked['metrics']['observed_all_time_min']['retailer_key'], 'rei')
        self.assertIsNone(list_watches(self.db)[0]['metrics']['observed_all_time_min'])
        self.assertEqual(self.db.execute('SELECT retailer_key FROM retailers').fetchone()[0], 'rei')


if __name__ == '__main__':
    unittest.main()
