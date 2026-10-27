"""Host-level website blocks pause requests without inventing variant failures."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from arcteryx_prices.collector import CollectorError
from arcteryx_prices.database import canonical_time, parse_time
from arcteryx_prices.monitor import (
    check_watch, create_watch, discover_for_monitor, initialize_monitor, list_watches,
    run_due, update_watch,
)

BASE = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
REI = 'https://www.rei.com/product/999990/arcteryx-synthetic-shell-mens'
OFFICIAL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'


def quote(watch, at):
    return {key: watch[key] for key in ('sku', 'color', 'size', 'market', 'currency')} | {
        'source_url': watch['url'], 'price_minor': 40000, 'availability': 'in_stock',
        'observed_at': canonical_time(at), 'captured_at': canonical_time(at),
        'evidence_sha256': 'a' * 64,
    }


class BlockedBackoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize_monitor(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def watch(self, index=0, url=REI):
        return create_watch(self.db, {'url': url, 'sku': f'SYNTHETIC-{index}',
            'color': 'Black', 'size': 'XL', 'rule': 'new_low', 'interval_minutes': 15})

    def blocked(self, watch, at=BASE, code=403, retry_after=None):
        return check_watch(self.db, watch['id'], now=at, fetcher=Mock(side_effect=CollectorError(
            'site_blocked', 'Retailer blocked automated requests.', http_status=code,
            retry_after=retry_after)))

    def host(self):
        row = self.db.execute("SELECT * FROM monitor_host_cooldowns WHERE host='www.rei.com'").fetchone()
        return dict(row) if row else None

    def test_twenty_variants_stop_after_first_block_and_other_retailers_still_run(self):
        for index in range(20):
            self.watch(index, REI.replace('999990', str(999990 + index // 4)))
        official = self.watch(20, OFFICIAL)
        def collect(watch):
            if watch['retailer_key'] == 'rei':
                raise CollectorError('site_blocked', 'Retailer blocked automated requests.', http_status=403)
            return quote(watch, BASE)
        fetcher = Mock(side_effect=collect)
        results = run_due(self.db, fetcher=fetcher, now=BASE)
        self.assertEqual(fetcher.call_count, 2, 'one blocked REI request and one independent official request')
        self.assertEqual(sum(bool(result['checks']) for result in results), 2)
        self.assertTrue(all(result['check_status'] == 'site_blocked' for result in results[:20]))
        self.assertEqual(results[-1]['id'], official['id'])
        self.assertEqual(results[-1]['check_status'], 'ok')
        self.assertEqual(self.db.execute('SELECT SUM(consecutive_failures) FROM monitor_watches').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 1)
        self.assertEqual(self.host()['consecutive_blocks'], 1)
        skipped = self.db.execute('SELECT last_status,last_checked_at FROM monitor_watches WHERE id=2').fetchone()
        self.assertEqual(tuple(skipped), ('never_checked', None))
        displayed = list_watches(self.db, now=BASE)[1]
        self.assertEqual(displayed['status'], 'site_blocked')
        self.assertIn('all its watches', displayed['last_error'])
        self.assertIsNone(displayed['current_price_minor'])

    def test_restart_force_manual_discovery_and_settings_cannot_bypass_wait(self):
        first, second = self.watch(), self.watch(1)
        self.blocked(first)
        self.db.close()
        self.db = initialize_monitor(self.path)
        update_watch(self.db, second['id'], {'enabled': False})
        update_watch(self.db, second['id'], {'enabled': True, 'interval_minutes': 15})
        never = Mock(side_effect=AssertionError('cooldown must prevent requests'))
        at = BASE + timedelta(minutes=30)
        self.assertEqual(check_watch(self.db, second['id'], fetcher=never, now=at)['check_status'], 'site_blocked')
        results = run_due(self.db, fetcher=never, now=at, force=True)
        self.assertTrue(all(not result['checks'] for result in results))
        with patch('arcteryx_prices.collector.discover_variants', never), self.assertRaises(CollectorError) as raised:
            discover_for_monitor(self.db, REI, now=at)
        self.assertEqual(raised.exception.status, 'site_blocked')
        self.assertEqual(raised.exception.retry_after, BASE + timedelta(hours=1))
        never.assert_not_called()
        self.assertEqual(self.host()['blocked_since'], canonical_time(BASE))
        self.assertEqual(self.host()['consecutive_blocks'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_checks').fetchone()[0], 1)

    def test_host_progression_uses_actual_block_attempts_even_across_different_watches(self):
        at = BASE
        for index, hours in enumerate((1, 2, 4, 8, 16, 24, 24)):
            with self.subTest(attempt=index):
                watch = self.watch(index)
                result = self.blocked(watch, at=at, code=200 if index % 2 else 403)
                self.assertEqual(result['check_status'], 'site_blocked')
                self.assertTrue(result['checks'][0]['retryable'])
                self.assertEqual(parse_time(result['next_retry_at']) - at, timedelta(hours=hours))
                host = self.host()
                self.assertEqual(host['consecutive_blocks'], index + 1)
                self.assertEqual(host['blocked_since'], canonical_time(BASE))
                self.assertEqual(host['status'], 'site_blocked')
                at = parse_time(host['not_before'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 0)

    def test_retry_after_is_capped_and_rate_limit_does_not_create_new_block_episode(self):
        watch = self.watch()
        first = self.blocked(watch, retry_after=BASE + timedelta(days=500))
        self.assertEqual(parse_time(first['next_retry_at']), BASE + timedelta(hours=24))
        at = BASE + timedelta(hours=24)
        error = CollectorError('http_error', 'Rate limited.', http_status=429,
                               retry_after=at + timedelta(hours=2))
        check_watch(self.db, watch['id'], fetcher=Mock(side_effect=error), now=at)
        host = self.host()
        self.assertEqual(host['status'], 'site_blocked')
        self.assertEqual(host['blocked_since'], canonical_time(BASE))
        self.assertEqual(host['consecutive_blocks'], 1)
        self.assertEqual(host['http_status'], 429)
        self.assertEqual(parse_time(host['not_before']), at + timedelta(hours=2))

    def test_success_clears_episode_and_next_block_starts_at_one_hour(self):
        watch = self.watch()
        self.blocked(watch)
        at = BASE + timedelta(hours=1)
        self.assertEqual(self.host()['status'], 'site_blocked', 'expiry alone is not recovery')
        recovered = check_watch(self.db, watch['id'], fetcher=lambda w: quote(w, at), now=at)
        self.assertEqual(recovered['check_status'], 'ok')
        self.assertIsNone(self.host())
        self.assertEqual(recovered['consecutive_failures'], 0)
        self.assertIsNone(recovered['alert'])
        at += timedelta(minutes=15)
        next_block = self.blocked(watch, at=at)
        self.assertEqual(self.host()['blocked_since'], canonical_time(at))
        self.assertEqual(self.host()['consecutive_blocks'], 1)
        self.assertEqual(parse_time(next_block['next_retry_at']) - at, timedelta(hours=1))

    def test_failed_recovery_probe_keeps_other_variants_waiting(self):
        first, second = self.watch(), self.watch(1)
        self.blocked(first)
        at = BASE + timedelta(hours=1)
        error = CollectorError('network_error', 'Connection failed.')
        fetcher = Mock(side_effect=error)
        result = run_due(self.db, fetcher=fetcher, now=at)
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(result[1]['id'], second['id'])
        self.assertEqual(result[1]['check_status'], 'site_blocked')
        self.assertEqual(result[1]['checks'], [])
        self.assertEqual(self.host()['consecutive_blocks'], 1)
        self.assertEqual(self.host()['blocked_since'], canonical_time(BASE))
        self.assertEqual(parse_time(self.host()['not_before']), at + timedelta(hours=1))

    def test_stale_cached_quote_and_other_host_success_do_not_clear_episode(self):
        watch = self.watch()
        self.blocked(watch)
        official = self.watch(1, OFFICIAL)
        check_watch(self.db, official['id'], fetcher=lambda w: quote(w, BASE), now=BASE)
        self.assertIsNotNone(self.host())
        at = BASE + timedelta(hours=1)
        check_watch(self.db, watch['id'], fetcher=lambda w: quote(w, BASE - timedelta(minutes=1)), now=at)
        self.assertEqual(self.host()['blocked_since'], canonical_time(BASE))
        self.assertEqual(self.host()['consecutive_blocks'], 1)

    def test_discovery_block_is_shared_and_successful_discovery_recovers(self):
        error = CollectorError('site_blocked', 'Challenge page.', http_status=200)
        with patch('arcteryx_prices.collector.discover_variants', side_effect=error), self.assertRaises(CollectorError) as raised:
            discover_for_monitor(self.db, REI, now=BASE)
        self.assertEqual(raised.exception.retry_after, BASE + timedelta(hours=1))
        self.assertEqual(self.host()['http_status'], 200)
        watch = self.watch()
        never = Mock(side_effect=AssertionError('new watch must respect discovery cooldown'))
        self.assertEqual(check_watch(self.db, watch['id'], fetcher=never, now=BASE)['check_status'], 'site_blocked')
        never.assert_not_called()
        with patch('arcteryx_prices.collector.discover_variants', return_value={'variants': [{'sku': 'SYNTHETIC'}]}):
            discover_for_monitor(self.db, REI, now=BASE + timedelta(hours=1))
        self.assertIsNone(self.host())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 0)

    def test_old_cooldown_table_migrates_without_relabeling_rate_limits(self):
        self.db.execute('DROP TABLE monitor_host_cooldowns')
        self.db.execute('''CREATE TABLE monitor_host_cooldowns
            (host TEXT PRIMARY KEY,not_before TEXT NOT NULL,http_status INTEGER NOT NULL,updated_at TEXT NOT NULL)''')
        self.db.execute('INSERT INTO monitor_host_cooldowns VALUES (?,?,?,?)',
                        ('www.rei.com', canonical_time(BASE + timedelta(hours=2)), 429, canonical_time(BASE)))
        self.db.commit()
        self.db.close()
        self.db = initialize_monitor(self.path)
        host = self.host()
        self.assertEqual(host['status'], 'rate_limited')
        self.assertEqual(host['consecutive_blocks'], 0)
        self.assertIsNone(host['blocked_since'])
        self.assertEqual(host['not_before'], canonical_time(BASE + timedelta(hours=2)))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
