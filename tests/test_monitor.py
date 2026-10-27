"""Monitor integration tests use synthetic quotes and never access the network."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from arcteryx_prices.monitor import (
    check_watch, create_watch, initialize_monitor, list_alerts, list_watches,
    run_due, update_watch,
)
from arcteryx_prices import monitor as monitor_module


BASE = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
US_URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
CA_URL = 'https://arcteryx.com/ca/en/shop/mens/synthetic-shell'


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'arcteryx.com' or parsed.username:
        raise ValueError('unsupported product URL')
    if parsed.path.startswith('/us/en/shop/'):
        return {'url': url, 'market': 'US', 'currency': 'USD'}
    if parsed.path.startswith('/ca/en/shop/'):
        return {'url': url, 'market': 'CA', 'currency': 'CAD'}
    raise ValueError('unsupported market')


def quote(price=45000, stock='in_stock', at=BASE, **changes):
    return {
        'price_minor': price, 'availability': stock,
        'observed_at': at.isoformat(), 'captured_at': at.isoformat(),
        'source_url': US_URL,
        'evidence_sha256': hashlib.sha256(str((price,stock,at)).encode()).hexdigest(),
        'sku': 'SYNTHETIC-SKU-M', 'color': 'Black', 'size': 'M',
        'market': 'US', 'currency': 'USD',
    } | changes


class FetchError(Exception):
    def __init__(self, status='network_error'):
        self.status = status
        super().__init__(f'SYNTHETIC {status}')


class SequenceFetcher:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0

    def __call__(self, watch):
        self.calls += 1
        if not self.items:
            raise AssertionError('unexpected synthetic fetch')
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item)


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'prices.sqlite3'
        self.connection = initialize_monitor(self.path)
        self.validator = patch('arcteryx_prices.monitor._validate_url', side_effect=validate_url)
        self.validator.start()

    def tearDown(self):
        self.validator.stop()
        self.connection.close()
        self.temporary.cleanup()

    def create(self, **changes):
        return create_watch(self.connection, {
            'url': US_URL, 'sku': 'SYNTHETIC-SKU-M', 'color': 'Black', 'size': 'M',
            'label': 'Synthetic watch', 'rule': 'target', 'target_price_minor': 50000,
        } | changes)

    def count(self, table):
        return self.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def check(self, watch, price=45000, stock='in_stock', hour=0, **changes):
        at = BASE + timedelta(hours=hour)
        result = quote(price, stock, at=at, **changes)
        fetcher = SequenceFetcher(result, result)
        return check_watch(self.connection, watch['id'], fetcher, now=at), fetcher

    def test_creating_watch_does_not_create_prices_or_invent_history(self):
        watch = self.create()
        self.assertEqual(self.count('products'), 0)
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(watch['status'], 'never_checked')
        self.assertIsNone(watch['current_price_minor'])
        self.assertIsNone(watch['metrics']['observed_all_time_min'])
        with self.assertRaisesRegex(ValueError, 'already watched'):
            self.create()

    def test_target_twice_verified_dedupes_until_price_rebounds(self):
        watch = self.create()
        first, fetcher = self.check(watch)
        self.assertEqual(fetcher.calls, 2)
        self.assertEqual(first['alert']['price_minor'], 45000)
        self.assertEqual(first['current_price_minor'], 45000)
        lower, fetcher = self.check(watch, 40000, hour=1)
        self.assertEqual(fetcher.calls, 1)
        self.assertIsNone(lower['alert'])
        self.check(watch, 60000, hour=2)
        repeat, fetcher = self.check(watch, 49000, hour=3)
        self.assertEqual(fetcher.calls, 2)
        self.assertIsNotNone(repeat['alert'])
        self.assertEqual(len(list_alerts(self.connection)), 2)

    def test_failure_invalidates_current_price_preserves_success_and_dedup(self):
        watch = self.create()
        first, _ = self.check(watch)
        failure = check_watch(self.connection, watch['id'], SequenceFetcher(FetchError()), now=BASE+timedelta(hours=1))
        self.assertEqual(failure['status'], 'network_error')
        self.assertTrue(failure['stale'])
        self.assertIsNone(failure['current_price_minor'])
        self.assertEqual(failure['last_success_at'], first['last_success_at'])
        self.assertEqual(failure['availability'], 'in_stock')
        recovered, fetcher = self.check(watch, hour=2)
        self.assertEqual(fetcher.calls, 1)
        self.assertIsNone(recovered['alert'])
        self.assertEqual(self.count('monitor_alerts'), 1)

    def test_success_expires_and_out_of_stock_never_displays_buyable_price(self):
        watch = self.create(target_price_minor=100)
        first, _ = self.check(watch)
        fresh = list_watches(self.connection, now=BASE+timedelta(minutes=59))[0]
        expired = list_watches(self.connection, now=BASE+timedelta(minutes=61))[0]
        self.assertEqual(fresh['current_price_minor'], 45000)
        self.assertIsNone(expired['current_price_minor'])
        self.assertTrue(expired['stale'])
        absent, _ = self.check(watch, 45000, 'out_of_stock', hour=2)
        self.assertEqual(absent['availability'], 'out_of_stock')
        self.assertIsNone(absent['current_price_minor'])
        self.assertFalse(absent['stale'])

    def test_changed_confirmation_and_failed_confirmation_do_not_create_history(self):
        watch = self.create()
        changed = check_watch(self.connection, watch['id'], SequenceFetcher(quote(40000),quote(41000)), now=BASE)
        self.assertEqual(changed['status'], 'verification_changed')
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(self.count('monitor_alerts'), 0)
        self.assertIsNone(changed['last_success_at'])
        at = BASE+timedelta(hours=1)
        failed = check_watch(self.connection, watch['id'], SequenceFetcher(quote(at=at),FetchError('http_error')), now=at)
        self.assertEqual(failed['status'], 'verification_failed')
        self.assertEqual(failed['checks'][1]['status'], 'http_error')
        self.assertEqual(self.count('monitor_checks'), 4)
        self.assertEqual(self.count('price_observations'), 0)
        valid, _ = self.check(watch, hour=2)
        self.assertIsNotNone(valid['alert'])

    def test_new_low_requires_prior_history_and_dedupes_same_sale(self):
        watch = self.create(rule='new_low',target_price_minor=None)
        initial, fetcher = self.check(watch, 60000)
        self.assertIsNone(initial['alert'])
        self.assertEqual(fetcher.calls, 1)
        low, fetcher = self.check(watch, 50000, hour=1)
        self.assertIsNotNone(low['alert'])
        self.assertEqual(fetcher.calls, 2)
        continuing, _ = self.check(watch, 40000, hour=2)
        self.assertIsNone(continuing['alert'])
        self.check(watch, 65000, hour=3)
        above_record, _ = self.check(watch, 45000, hour=4)
        self.assertIsNone(above_record['alert'])
        record, _ = self.check(watch, 35000, hour=5)
        self.assertIsNotNone(record['alert'])
        self.assertEqual(self.count('monitor_alerts'), 2)
        self.assertEqual(record['metrics']['observed_all_time_min']['price_minor'], 35000)

    def test_restock_requires_explicit_stock_transition_and_optional_target(self):
        watch = self.create(rule='restock')
        initial, _ = self.check(watch)
        self.assertIsNone(initial['alert'])
        self.check(watch, None, 'out_of_stock', hour=1)
        check_watch(self.connection, watch['id'], SequenceFetcher(FetchError('parse_error')), now=BASE+timedelta(hours=2))
        restored, fetcher = self.check(watch, 49000, hour=3)
        self.assertIsNotNone(restored['alert'])
        self.assertEqual(fetcher.calls, 2)
        self.check(watch, None, 'out_of_stock', hour=4)
        expensive, _ = self.check(watch, 55000, hour=5)
        self.assertIsNone(expensive['alert'])
        later_discount, _ = self.check(watch, 49000, hour=6)
        self.assertIsNone(later_discount['alert'])
        self.assertEqual(self.count('monitor_alerts'), 1)

    def test_markets_are_inferred_and_cannot_be_overridden(self):
        us = self.create()
        self.assertEqual((us['market'],us['currency']), ('US','USD'))
        with self.assertRaisesRegex(ValueError, 'Only US/USD'):
            self.create(url=CA_URL)
        with self.assertRaisesRegex(ValueError, 'unknown watch fields'):
            self.create(currency='CAD')
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            self.create(url='https://example.com/us/en/shop/shell')
        at = BASE
        q = quote(45000, at=at, source_url=CA_URL,market='CA',currency='CAD')
        result = check_watch(self.connection, us['id'], SequenceFetcher(q), now=at)
        self.assertEqual(result['status'], 'parse_error')
        self.assertIsNone(result['alert'])
        us_public = list_watches(self.connection, now=at)[0]
        self.assertIsNone(us_public['metrics']['observed_all_time_min'])

    def test_legacy_canadian_watch_is_retired_without_deleting_prices_or_alerts(self):
        watch = self.create()
        self.check(watch)
        # Simulate a database created by the former US/CA release.
        with self.connection:
            self.connection.execute('''UPDATE monitor_watches SET url=?,market='CA',currency='CAD',
                lease_token='legacy-lease',lease_until='2999-01-01T00:00:00Z' WHERE id=?''', (CA_URL,watch['id']))
            self.connection.execute("UPDATE offers SET market='CA',currency='CAD'")
            self.connection.execute('UPDATE price_observations SET source_url=?', (CA_URL,))
            self.connection.execute("UPDATE monitor_alerts SET currency='CAD',url=?", (CA_URL,))
        originals = {table: [tuple(row) for row in self.connection.execute(f'SELECT * FROM {table}')]
                     for table in ('price_observations', 'monitor_alerts', 'offers', 'products', 'variants')}
        version = self.connection.execute('SELECT config_version FROM monitor_watches').fetchone()[0]
        fetcher = SequenceFetcher()
        # Both manual and scheduled entry points reject it even before migration.
        self.assertEqual(run_due(self.connection, fetcher, now=BASE, force=True), [])
        with self.assertRaisesRegex(ValueError, 'Only US/USD'):
            check_watch(self.connection, watch['id'], fetcher, now=BASE)
        with self.assertRaisesRegex(ValueError, 'Only US/USD'):
            update_watch(self.connection, watch['id'], {'enabled': True})
        self.assertEqual(fetcher.calls, 0)
        self.connection.close()
        self.connection = initialize_monitor(self.path)
        retired = dict(self.connection.execute('SELECT * FROM monitor_watches').fetchone())
        self.assertEqual(retired['enabled'], 0)
        self.assertEqual(retired['config_version'], version + 1)
        self.assertIsNone(retired['lease_token'])
        self.assertIsNone(retired['lease_until'])
        self.assertEqual(list_watches(self.connection), [])
        self.assertEqual(list_alerts(self.connection), [])
        for table, records in originals.items():
            self.assertEqual([tuple(row) for row in self.connection.execute(f'SELECT * FROM {table}')], records)
        self.connection.close()
        self.connection = initialize_monitor(self.path)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM monitor_watches').fetchone()), retired)

    def test_quote_identity_mismatch_is_parse_failure_not_a_price(self):
        watch = self.create()
        result = check_watch(self.connection, watch['id'], SequenceFetcher(quote(sku='WRONG-SKU')), now=BASE)
        self.assertEqual(result['status'], 'parse_error')
        self.assertIsNone(result['current_price_minor'])
        self.assertEqual(self.count('price_observations'), 0)

    def test_collector_failure_classification_is_preserved(self):
        watch = self.create()
        result = check_watch(self.connection, watch['id'], SequenceFetcher(FetchError('identity_mismatch')), now=BASE)
        self.assertEqual(result['status'], 'identity_mismatch')
        self.assertIsNone(result['last_success_at'])
        self.assertEqual(self.count('price_observations'), 0)

    def test_real_url_validation_becomes_input_error(self):
        self.validator.stop()
        with self.assertRaises(ValueError):
            self.create(url='https://example.com/us/en/shop/mens/synthetic-shell')

    def test_pause_due_scheduling_and_resume_do_not_send_external_messages(self):
        watch = self.create(target_price_minor=100)
        result = run_due(self.connection, SequenceFetcher(quote()), now=BASE)
        self.assertEqual(len(result), 1)
        self.assertEqual(run_due(self.connection, SequenceFetcher(), now=BASE+timedelta(minutes=59)), [])
        update_watch(self.connection, watch['id'], {'enabled':False})
        self.assertEqual(run_due(self.connection, SequenceFetcher(), now=BASE+timedelta(hours=2),force=True), [])
        paused = check_watch(self.connection, watch['id'], SequenceFetcher(), now=BASE+timedelta(hours=2))
        self.assertEqual(paused['check_status'], 'paused')
        self.assertIsNone(paused['current_price_minor'])
        update_watch(self.connection, watch['id'], {'enabled':True})
        at=BASE+timedelta(hours=3)
        self.assertEqual(len(run_due(self.connection, SequenceFetcher(quote(at=at)), now=at)), 1)

    def test_parallel_worker_lease_prevents_duplicate_fetch_and_event(self):
        watch = self.create()
        other = initialize_monitor(self.path)
        outcomes = []
        def fetch(w):
            outcomes.append(check_watch(other, watch['id'], SequenceFetcher(), now=BASE))
            return quote()
        try:
            first = check_watch(self.connection, watch['id'], fetch, now=BASE)
        finally:
            other.close()
        self.assertTrue(all(item['check_status']=='busy' for item in outcomes))
        self.assertEqual(first['check_status'], 'ok')
        self.assertEqual(self.count('monitor_alerts'), 1)

    def test_scheduler_rechecks_due_atomically_after_another_check_finishes(self):
        watch = self.create(target_price_minor=100)
        original = monitor_module._check_watch
        def another_worker_completed(connection, watch_id, **kwargs):
            # run_due already selected this watch, but a manual/other worker
            # check finishes before the scheduler attempts to claim its lease.
            original(connection, watch_id, fetcher=SequenceFetcher(quote()), now=BASE)
            return original(connection, watch_id, **kwargs)
        with patch('arcteryx_prices.monitor._check_watch', side_effect=another_worker_completed):
            result = run_due(self.connection, SequenceFetcher(), now=BASE)
        self.assertEqual(result, [])
        self.assertEqual(self.count('monitor_checks'), 1)

    def test_configuration_changed_during_fetch_discards_candidate(self):
        watch = self.create()
        def fetch(w):
            update_watch(self.connection, watch['id'], {'enabled':False})
            return quote()
        result = check_watch(self.connection, watch['id'], fetch, now=BASE)
        self.assertEqual(result['check_status'], 'configuration_changed')
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(self.count('monitor_alerts'), 0)

    def test_bad_import_cannot_leave_success_or_half_created_alert(self):
        watch = self.create()
        self.check(watch)
        # Same offer/time with a conflicting source value cannot overwrite history.
        result = check_watch(self.connection, watch['id'], SequenceFetcher(quote(44000)), now=BASE+timedelta(minutes=1))
        self.assertEqual(result['check_status'], 'storage_error')
        self.assertIsNone(result['current_price_minor'])
        self.assertEqual(self.count('monitor_alerts'), 1)
        self.assertEqual(self.count('price_observations'), 1)

    def test_settings_validation(self):
        for change in ({'interval_minutes':14},{'interval_minutes':1441},
                       {'interval_minutes':True},{'rule':'invalid'},
                       {'target_price_minor':None},{'target_price_minor':1.5}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.create(**change)
        watch=self.create()
        with self.assertRaises(ValueError):
            update_watch(self.connection,watch['id'],{'enabled':1})


if __name__ == '__main__':
    unittest.main()
