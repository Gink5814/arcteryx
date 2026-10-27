"""Resilience checks use synthetic pages/quotes and isolated SQLite databases."""

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from arcteryx_prices.collector import BatchQuoteCollector, CollectorError
from arcteryx_prices.database import canonical_time, parse_time
from arcteryx_prices.monitor import (
    check_watch, create_watch, discover_for_monitor, initialize_monitor,
    list_watches, run_due, update_watch,
)

BASE = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
OUTLET = 'https://outlet.arcteryx.com/us/en/shop/mens/synthetic-shell'
REI = 'https://www.rei.com/product/999999/arcteryx-synthetic-shell'
BODY = (Path(__file__).parent / 'fixtures/collector_synthetic.html').read_bytes()


def quote(watch, at=BASE, price=49995):
    return {key: watch[key] for key in ('sku', 'color', 'size', 'market', 'currency')} | {
        'source_url': watch['url'], 'price_minor': price, 'availability': 'in_stock',
        'observed_at': canonical_time(at), 'captured_at': canonical_time(at),
        'evidence_sha256': hashlib.sha256(b'SYNTHETIC RESILIENCE QUOTE').hexdigest(),
    }


class ResilienceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'synthetic.sqlite3'
        self.db = initialize_monitor(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def watch(self, size='M', **changes):
        return create_watch(self.db, {
            'url': URL, 'sku': f'SYNTHETIC-BLACK-{size}', 'color': 'Black',
            'size': size, 'rule': 'new_low', 'interval_minutes': 60,
        } | changes)

    def count(self, table):
        return self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def fail(self, watch, *, at=BASE, code=None, retry_after=None, status='http_error', response_url=None):
        fetcher = Mock(side_effect=CollectorError(status, 'SYNTHETIC source failure',
                       http_status=code, retry_after=retry_after, response_url=response_url))
        result = check_watch(self.db, watch['id'], fetcher=fetcher, now=at)
        self.assertEqual(fetcher.call_count, 1)
        return result

    def test_same_page_primary_is_shared_but_distinct_skus_and_stock_are_preserved(self):
        self.watch('M')
        self.watch('L')
        with patch('arcteryx_prices.collector._read_page', return_value=(BODY, URL)) as fetch:
            result = run_due(self.db)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual([r['last_price_minor'] for r in result], [49995, 24995])
            self.assertEqual([r['availability'] for r in result], ['in_stock', 'out_of_stock'])
            self.assertEqual(result[0]['last_success_at'], result[1]['last_success_at'])
            run_due(self.db, force=True)
            self.assertEqual(fetch.call_count, 2, 'a new scheduling cycle must fetch a new page')
        self.assertEqual(self.count('monitor_alerts'), 0)

    def test_confirmation_fetch_is_fresh_and_changed_price_cannot_create_alert(self):
        self.watch('M', rule='target', target_price_minor=50000)
        self.watch('L')
        changed = BODY.replace(b'499.95', b'399.95')
        with patch('arcteryx_prices.collector._read_page', side_effect=[(BODY, URL), (changed, URL)]) as fetch:
            results = run_due(self.db)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(results[0]['check_status'], 'verification_changed')
        self.assertEqual(results[1]['last_price_minor'], 24995)
        self.assertEqual(self.count('monitor_alerts'), 0)
        self.assertEqual(self.count('price_observations'), 1)  # out-of-stock L only

    def test_delayed_cached_primary_does_not_renew_quote_freshness(self):
        first, second = self.watch('M'), self.watch('L')
        available = BODY.replace(b'OutOfStock', b'InStock')
        batch = BatchQuoteCollector()
        with patch('arcteryx_prices.collector._read_page', return_value=(available, URL)) as fetch:
            a = check_watch(self.db, first['id'], fetcher=batch)
            later = parse_time(a['last_success_at']) + timedelta(hours=2)
            b = check_watch(self.db, second['id'], fetcher=batch, now=later)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(b['last_success_at'], a['last_success_at'])
        self.assertEqual(b['last_checked_at'], canonical_time(later))
        self.assertTrue(b['stale'])
        self.assertIsNone(b['current_price_minor'])

    def test_retry_after_persists_and_blocks_manual_scheduled_and_discovery_requests(self):
        first, second = self.watch('M'), self.watch('L')
        until = BASE + timedelta(days=1)
        result = self.fail(first, code=429, retry_after=BASE + timedelta(days=2))
        self.assertEqual(result['consecutive_failures'], 1)
        self.assertEqual(result['next_retry_at'], canonical_time(until))
        self.assertEqual(result['checks'][0]['http_status'], 429)
        self.db.close()
        self.db = initialize_monitor(self.path)
        fetcher = Mock(side_effect=AssertionError('cooling source must not be fetched'))
        for watch in (first, second):
            blocked = check_watch(self.db, watch['id'], fetcher=fetcher, now=BASE + timedelta(hours=1))
            self.assertEqual(blocked['check_status'], 'rate_limited')
            self.assertEqual(blocked['checks'], [])
        run_due(self.db, fetcher=fetcher, now=BASE + timedelta(hours=1), force=True)
        fetcher.assert_not_called()
        with patch('arcteryx_prices.collector.discover_variants') as discovery:
            with self.assertRaises(CollectorError) as raised:
                discover_for_monitor(self.db, URL, now=BASE + timedelta(hours=1))
            self.assertEqual(raised.exception.status, 'rate_limited')
            self.assertIsNone(raised.exception.http_status, 'a local skip is not a remote HTTP response')
            discovery.assert_not_called()
        self.assertEqual(self.count('monitor_checks'), 1)
        self.assertEqual(self.count('price_observations'), 0)

    def test_injected_extreme_retry_after_is_bounded_for_watch_and_check_metadata(self):
        for code, url in ((408, URL), (429, REI), (503, OUTLET)):
            with self.subTest(code=code):
                watch = self.watch(url=url)
                result = self.fail(watch, code=code,
                                   retry_after=datetime.max.replace(tzinfo=timezone.utc))
                ceiling = canonical_time(BASE + timedelta(hours=24))
                self.assertEqual(result['retry_not_before'], ceiling)
                self.assertEqual(result['next_due_at'], ceiling)
                self.assertEqual(result['checks'][0]['retry_after'], ceiling)
                self.assertEqual(result['consecutive_failures'], 1)
                if code in (429, 503):
                    self.assertEqual(result['cooldown_until'], ceiling)
                else:
                    self.assertIsNone(result['cooldown_until'])

    def test_discovery_caps_extreme_deadline_and_can_resume_after_24_hours(self):
        ceiling = BASE + timedelta(hours=24)
        error = CollectorError('http_error', 'SYNTHETIC 429', http_status=429,
                               retry_after=datetime.max.replace(tzinfo=timezone.utc))
        with patch('arcteryx_prices.collector.discover_variants', side_effect=error) as fetch:
            with self.assertRaises(CollectorError) as failure:
                discover_for_monitor(self.db, URL, now=BASE)
            self.assertEqual(failure.exception.retry_after, ceiling)
            with self.assertRaises(CollectorError) as blocked:
                discover_for_monitor(self.db, URL, now=ceiling - timedelta(seconds=1))
            self.assertEqual(blocked.exception.status, 'rate_limited')
            self.assertEqual(fetch.call_count, 1)
        with patch('arcteryx_prices.collector.discover_variants', return_value={'variants': []}) as fetch:
            self.assertEqual(discover_for_monitor(self.db, URL, now=ceiling), {'variants': []})
            fetch.assert_called_once()

    def test_old_unbounded_waits_are_repaired_from_failure_time_without_sliding_on_restart(self):
        watch = self.watch()
        check_watch(self.db, watch['id'], fetcher=lambda w: quote(w), now=BASE - timedelta(hours=1))
        self.fail(watch, code=429, at=BASE, retry_after=BASE + timedelta(hours=1))
        normal = self.watch(url=REI)
        self.fail(normal, code=503, at=BASE, retry_after=BASE + timedelta(hours=4))
        normal_before = dict(self.db.execute('SELECT * FROM monitor_watches WHERE id=?', (normal['id'],)).fetchone())
        old_deadline = '2029-12-01T00:00:00.000000Z'
        with self.db:
            self.db.execute('''UPDATE monitor_watches SET retry_not_before=?,next_due_at=? WHERE id=?''',
                            (old_deadline, old_deadline, watch['id']))
            self.db.execute("UPDATE monitor_host_cooldowns SET not_before=? WHERE host='arcteryx.com'", (old_deadline,))
            self.db.execute('UPDATE monitor_checks SET retry_after=? WHERE watch_id=? AND status<>?',
                            (old_deadline, watch['id'], 'ok'))
        checks_before = [tuple(row) for row in self.db.execute('SELECT * FROM monitor_checks')]
        observations_before = [tuple(row) for row in self.db.execute('SELECT * FROM price_observations')]
        for startup in (BASE + timedelta(hours=2), BASE + timedelta(hours=25)):
            self.db.close()
            with patch('arcteryx_prices.monitor._now', return_value=startup):
                self.db = initialize_monitor(self.path)
            repaired = dict(self.db.execute('SELECT * FROM monitor_watches WHERE id=?', (watch['id'],)).fetchone())
            self.assertEqual(repaired['retry_not_before'], canonical_time(BASE + timedelta(hours=24)))
            self.assertEqual(repaired['next_due_at'], repaired['retry_not_before'])
            self.assertEqual(checks_before, [tuple(row) for row in self.db.execute('SELECT * FROM monitor_checks')])
            self.assertEqual(observations_before, [tuple(row) for row in self.db.execute('SELECT * FROM price_observations')])
            self.assertEqual(normal_before, dict(self.db.execute('SELECT * FROM monitor_watches WHERE id=?', (normal['id'],)).fetchone()))
        blocked = Mock()
        self.assertEqual(check_watch(self.db, watch['id'], fetcher=blocked,
                                    now=BASE + timedelta(hours=23))['check_status'], 'rate_limited')
        blocked.assert_not_called()
        resumed = BASE + timedelta(hours=24)
        results = run_due(self.db, fetcher=lambda w: quote(w, at=resumed), now=resumed)
        self.assertTrue(any(result['id'] == watch['id'] and result['check_status'] == 'ok' for result in results))

    def test_host_cooldown_does_not_block_another_retailer_or_rewrite_unattempted_quote(self):
        first, second = self.watch('M'), self.watch('L')
        other = self.watch('M', url=REI)
        check_watch(self.db, second['id'], fetcher=lambda w: quote(w), now=BASE)
        prior = dict(self.db.execute('SELECT * FROM monitor_watches WHERE id=?', (second['id'],)).fetchone())
        self.fail(first, code=429, retry_after=BASE + timedelta(hours=2), at=BASE + timedelta(minutes=5))
        blocked = check_watch(self.db, second['id'], fetcher=Mock(), now=BASE + timedelta(minutes=10))
        after = dict(self.db.execute('SELECT * FROM monitor_watches WHERE id=?', (second['id'],)).fetchone())
        self.assertEqual(prior, after)
        self.assertEqual(blocked['current_price_minor'], 49995)
        self.assertEqual(blocked['check_status'], 'rate_limited')
        ok = check_watch(self.db, other['id'], fetcher=lambda w: quote(w), now=BASE)
        self.assertEqual(ok['check_status'], 'ok')

    def test_retryable_failures_back_off_then_success_resets_without_false_alert(self):
        watch = self.watch(rule='target', target_price_minor=50000)
        initial = check_watch(self.db, watch['id'], fetcher=lambda w: quote(w), now=BASE)
        self.assertIsNotNone(initial['alert'])
        at = BASE + timedelta(hours=1)
        for count, hours in ((1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 24), (7, 24)):
            failed = self.fail(watch, at=at, status='network_error')
            self.assertEqual(failed['consecutive_failures'], count)
            self.assertEqual(parse_time(failed['next_retry_at']) - at, timedelta(hours=hours))
            self.assertIsNone(failed['current_price_minor'])
            blocked = Mock(side_effect=AssertionError('backoff must prevent immediate retry'))
            self.assertEqual(check_watch(self.db, watch['id'], fetcher=blocked, now=at)['check_status'], 'backoff')
            blocked.assert_not_called()
            at += timedelta(hours=hours)
        recovered = check_watch(self.db, watch['id'], fetcher=lambda w: quote(w, at=at), now=at)
        self.assertEqual(recovered['consecutive_failures'], 0)
        self.assertIsNone(recovered['retry_not_before'])
        self.assertIsNone(recovered['next_retry_at'])
        self.assertIsNone(recovered['alert'])
        self.assertEqual(self.count('monitor_alerts'), 1)

    def test_parse_identity_and_nonretryable_http_failures_keep_normal_interval(self):
        for index, (status, code) in enumerate((('identity_mismatch', None), ('parse_error', None), ('http_error', 404))):
            watch = self.watch(size=str(index))
            first = self.fail(watch, status=status, code=code)
            second = self.fail(watch, status=status, code=code)
            self.assertEqual(second['consecutive_failures'], 2)
            self.assertIsNone(second['retry_not_before'])
            self.assertEqual(parse_time(second['next_due_at']) - BASE, timedelta(hours=1))
            self.assertFalse(first['checks'][0]['retryable'])
        self.assertEqual(self.count('monitor_host_cooldowns'), 0)

    def test_pause_resume_and_interval_change_do_not_bypass_server_cooldown(self):
        watch = self.watch()
        until = BASE + timedelta(hours=4)
        self.fail(watch, code=503, retry_after=until)
        update_watch(self.db, watch['id'], {'enabled': False})
        update_watch(self.db, watch['id'], {'enabled': True, 'interval_minutes': 30})
        fetcher = Mock()
        result = check_watch(self.db, watch['id'], fetcher=fetcher, now=BASE + timedelta(hours=1))
        fetcher.assert_not_called()
        self.assertEqual(result['next_retry_at'], canonical_time(until))
        self.assertEqual(result['check_status'], 'rate_limited')

    def test_discovery_failure_creates_cooldown_without_watch_or_observation(self):
        until = BASE + timedelta(hours=3)
        error = CollectorError('http_error', 'SYNTHETIC 503', http_status=503, retry_after=until)
        with patch('arcteryx_prices.collector.discover_variants', side_effect=error) as fetch:
            with self.assertRaises(CollectorError):
                discover_for_monitor(self.db, URL, now=BASE)
            with self.assertRaises(CollectorError):
                discover_for_monitor(self.db, URL, now=BASE + timedelta(minutes=1))
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.count('monitor_watches'), 0)
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(self.count('monitor_alerts'), 0)

    def test_initial_discovery_error_reports_actual_persisted_cooldown_deadline(self):
        error = CollectorError('http_error', 'SYNTHETIC 429', http_status=429,
                               retry_after=BASE + timedelta(seconds=30))
        with patch('arcteryx_prices.collector.discover_variants', side_effect=error):
            with self.assertRaises(CollectorError) as first:
                discover_for_monitor(self.db, URL, now=BASE)
            with self.assertRaises(CollectorError) as second:
                discover_for_monitor(self.db, URL, now=BASE)
        self.assertEqual(first.exception.retry_after, BASE + timedelta(hours=1))
        self.assertEqual(first.exception.retry_after, second.exception.retry_after)
        self.assertEqual(first.exception.http_status, 429)

    def test_rate_limited_confirmation_preserves_alert_arming_without_partial_history(self):
        watch = self.watch(rule='target', target_price_minor=50000)
        error = CollectorError('http_error', 'SYNTHETIC 429', http_status=429,
                               retry_after=BASE + timedelta(hours=2))
        fetcher = Mock(side_effect=[quote(watch), error])
        result = check_watch(self.db, watch['id'], fetcher=fetcher, now=BASE)
        self.assertEqual(result['check_status'], 'verification_failed')
        self.assertEqual(result['consecutive_failures'], 1)
        self.assertEqual(result['next_retry_at'], canonical_time(BASE + timedelta(hours=2)))
        self.assertEqual(result['armed'], 1)
        self.assertEqual(self.count('price_observations'), 0)
        self.assertEqual(self.count('monitor_alerts'), 0)
        self.assertEqual(self.count('monitor_checks'), 2)

    def test_redirect_rate_limit_protects_requested_and_actual_hosts(self):
        watch, redirected = self.watch(), self.watch(url=OUTLET)
        self.fail(watch, code=429, retry_after=BASE + timedelta(hours=4), response_url=OUTLET)
        hosts = {r['host'] for r in self.db.execute('SELECT host FROM monitor_host_cooldowns')}
        self.assertEqual(hosts, {'arcteryx.com', 'outlet.arcteryx.com'})
        fetch = Mock()
        result = check_watch(self.db, redirected['id'], fetcher=fetch, now=BASE)
        self.assertEqual(result['check_status'], 'rate_limited')
        fetch.assert_not_called()

    def test_cooldown_published_by_another_worker_before_claim_is_respected(self):
        first, second = self.watch(), self.watch('L')
        other = initialize_monitor(self.path)
        try:
            error = CollectorError('http_error', 'SYNTHETIC 429', http_status=429,
                                   retry_after=BASE + timedelta(hours=2))
            check_watch(other, first['id'], fetcher=Mock(side_effect=error), now=BASE)
            fetch = Mock()
            self.assertEqual(check_watch(self.db, second['id'], fetcher=fetch, now=BASE)['check_status'], 'rate_limited')
            fetch.assert_not_called()
        finally:
            other.close()

    def test_existing_monitor_schema_migrates_without_rewriting_observations(self):
        watch = self.watch()
        check_watch(self.db, watch['id'], fetcher=lambda w: quote(w), now=BASE)
        before = [tuple(row) for row in self.db.execute('SELECT * FROM price_observations')]
        for table, columns in {'monitor_watches': ['consecutive_failures', 'retry_not_before'],
                               'monitor_checks': ['http_status', 'retryable', 'retry_after']}.items():
            for column in columns:
                self.db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        self.db.close()
        self.db = initialize_monitor(self.path)
        self.assertEqual(before, [tuple(row) for row in self.db.execute('SELECT * FROM price_observations')])
        self.assertEqual(list_watches(self.db, now=BASE)[0]['consecutive_failures'], 0)


if __name__ == '__main__':
    unittest.main()
