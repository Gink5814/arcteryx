"""Source outage mail uses synthetic database state and mocked delivery only."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from arcteryx_prices.database import canonical_time
from arcteryx_prices.monitor import create_watch, initialize_monitor, update_watch
from arcteryx_prices.notifications import GmailConfig, deliver_notifications
from arcteryx_prices.source_health import blocked_sources


BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
URL = 'https://www.rei.com/product/123456/arcteryx-synthetic-shell-mens'


class SourceHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = initialize_monitor(Path(self.temp.name) / 'prices.sqlite3')
        self.config = GmailConfig('sender@example.com', 'owner@example.com', 'abcdefghijklmnop')
        self.transport = patch('arcteryx_prices.notifications._send')
        self.send = self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.db.close()
        self.temp.cleanup()

    def watch(self, sku='TEST-XL', url=URL):
        return create_watch(self.db, {'url': url, 'sku': sku, 'color': 'Black', 'size': 'XL',
                                     'rule': 'new_low', 'interval_minutes': 15})

    def block(self, at=BASE, host='www.rei.com'):
        with self.db:
            self.db.execute('''INSERT OR REPLACE INTO monitor_host_cooldowns
                (host,not_before,http_status,updated_at,status,blocked_since,consecutive_blocks)
                VALUES (?,?,403,?,'site_blocked',?,1)''',
                (host, canonical_time(at + timedelta(hours=1)), canonical_time(at), canonical_time(at)))

    def deliver(self, at=BASE, **kwargs):
        return deliver_notifications(self.db, self.config, now=at, **kwargs)

    def test_first_source_block_previews_and_sends_one_notice_for_twenty_watches(self):
        watches = [self.watch(f'TEST-{number}') for number in range(20)]
        self.block()
        with self.db:
            self.db.execute("UPDATE monitor_watches SET last_status='site_blocked',consecutive_failures=1,last_error='private challenge token' WHERE id=?", (watches[0]['id'],))
        preview = self.deliver(dry_run=True)
        self.assertEqual((preview['would_send'], preview['sent']), (1, 0))
        self.send.assert_not_called()
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_deliveries'").fetchone())
        self.assertEqual(self.deliver()['sent'], 1)
        message = self.send.call_args.args[0]
        self.assertIn('被网站拦截', message['Subject'])
        self.assertIn('Affected enabled watches: 20', message.get_content())
        self.assertIn('coverage for this source is unavailable', message.get_content())
        self.assertNotIn('private challenge token', message.get_content())
        self.assertEqual(self.deliver(BASE + timedelta(hours=2))['sent'], 0)
        update_watch(self.db, watches[0]['id'], {'enabled': False})
        self.assertEqual(blocked_sources(self.db)[0]['affected_watches'], 19)
        self.assertEqual(self.deliver(BASE + timedelta(hours=3))['sent'], 0)
        self.send.assert_called_once()

    def test_source_recovery_stops_stale_watch_health_and_new_outage_rearms(self):
        self.watch()
        self.block()
        self.assertEqual(self.deliver()['sent'], 1)
        with self.db:
            self.db.execute("UPDATE monitor_watches SET last_status='site_blocked',consecutive_failures=3")
            self.db.execute('DELETE FROM monitor_host_cooldowns')
        self.assertEqual(self.deliver(BASE + timedelta(hours=1))['sent'], 0)
        self.block(BASE + timedelta(hours=2))
        self.assertEqual(self.deliver(BASE + timedelta(hours=2))['sent'], 1)
        keys = [row[0] for row in self.db.execute('SELECT event_key FROM notification_deliveries')]
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(key.startswith('health:source:www.rei.com:') for key in keys))

    def test_failed_source_mail_retries_once_and_recovery_cancels_pending(self):
        self.watch()
        self.block()
        self.send.side_effect = OSError('private SMTP response')
        self.assertEqual(self.deliver()['failed'], 1)
        self.assertEqual(self.deliver(BASE + timedelta(minutes=10))['pending'], 1)
        self.assertEqual(self.send.call_count, 1)
        self.send.side_effect = None
        self.assertEqual(self.deliver(BASE + timedelta(minutes=30))['sent'], 1)
        self.block(BASE + timedelta(hours=2))
        self.send.side_effect = OSError('private')
        self.assertEqual(self.deliver(BASE + timedelta(hours=2))['failed'], 1)
        with self.db:
            self.db.execute('DELETE FROM monitor_host_cooldowns')
        self.assertEqual(self.deliver(BASE + timedelta(hours=3))['pending'], 0)
        self.assertEqual(self.send.call_count, 3)

    def test_paused_and_non_us_watches_do_not_keep_source_notice_active(self):
        first, second = self.watch('A'), self.watch('B')
        self.block()
        update_watch(self.db, first['id'], {'enabled': False})
        with self.db:
            self.db.execute("UPDATE monitor_watches SET market='CA',currency='CAD' WHERE id=?", (second['id'],))
        self.assertEqual(blocked_sources(self.db), [])
        self.assertEqual(self.deliver()['sent'], 0)
        self.send.assert_not_called()

    def test_other_source_generic_health_keeps_three_failure_threshold(self):
        rei = self.watch()
        official = self.watch('OFFICIAL', 'https://arcteryx.com/us/en/shop/mens/synthetic-shell')
        self.block()
        with self.db:
            self.db.execute("UPDATE monitor_watches SET last_status='parse_error',consecutive_failures=3,last_checked_at=?", (canonical_time(BASE),))
        self.assertEqual(self.deliver()['sent'], 2)
        keys = {row[0] for row in self.db.execute('SELECT event_key FROM notification_deliveries')}
        self.assertTrue(any(key.startswith(f"health:{official['id']}:") for key in keys))
        self.assertFalse(any(key.startswith(f"health:{rei['id']}:") for key in keys))


if __name__ == '__main__':
    unittest.main()
