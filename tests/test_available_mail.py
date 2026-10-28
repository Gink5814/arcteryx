"""Confirmed stock/target alerts retain their actual stock and provenance."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arcteryx_prices.monitor import check_watch, create_watch, initialize_monitor
from arcteryx_prices.notifications import deliver_notifications


BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
URL = 'https://outlet.arcteryx.com/us/en/shop/mens/synthetic-hoody-1234'


class AvailableMailTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize_monitor(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def watch(self, target=None):
        return create_watch(self.db, {'url': URL, 'sku': 'SYNTHETIC-XXL',
            'color': 'Synthetic Blue', 'size': 'XXL', 'label': 'Synthetic hoody',
            'rule': 'available_or_target', 'target_price': target})

    def check(self, watch, availability, at=BASE):
        value = {key: watch[key] for key in ('sku', 'color', 'size', 'market', 'currency')} | {
            'price_minor': 28000, 'availability': availability, 'source_url': URL,
            'observed_at': at.isoformat(), 'captured_at': at.isoformat(), 'evidence_sha256': 'a' * 64}
        return check_watch(self.db, watch['id'], fetcher=lambda _: dict(value), now=at)

    def test_first_stock_only_check_previews_confirmed_stock_and_source(self):
        result = self.check(self.watch(), 'in_stock')
        self.assertEqual(len(result['checks']), 2)
        with patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
            preview = deliver_notifications(self.db, dry_run=True, now=BASE)
            smtp.assert_not_called()
        self.assertEqual(preview['would_send'], 1)
        message = preview['previews'][0]
        self.assertIn('stock or target alert', message['subject'])
        self.assertIn('In stock (first observation or restock)', message['body'])
        self.assertIn('Availability at confirmation: in_stock', message['body'])
        self.assertIn('Confirmed at (UTC): 2026-11-01T12:00:00', message['body'])
        self.assertIn(f'Source: {URL}', message['body'])

    def test_old_out_of_stock_target_notice_does_not_claim_later_restock(self):
        watch = self.watch('280.00')
        out = self.check(watch, 'out_of_stock')
        self.assertEqual(out['alert']['trigger_reason'], 'target')
        later = BASE + timedelta(hours=1)
        available = self.check(watch, 'in_stock', later)
        self.assertEqual(available['alert']['trigger_reason'], 'stock')
        self.db.close()
        self.db = initialize_monitor(self.path)
        with patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
            preview = deliver_notifications(self.db, dry_run=True, now=later)
            smtp.assert_not_called()
        self.assertEqual(preview['would_send'], 2)
        earlier, latest = preview['previews']
        self.assertIn('OUT OF STOCK', earlier['body'])
        self.assertIn('price-only notice, not a restock', earlier['body'])
        self.assertNotIn('was in stock', earlier['body'])
        self.assertIn('Availability at confirmation: out_of_stock', earlier['body'])
        self.assertIn('Availability at confirmation: in_stock', latest['body'])
        self.assertIn('was in stock', latest['body'])
        for item in (earlier, latest):
            self.assertIn('USD 280.00', item['body'])
            self.assertIn(f'Source: {URL}', item['body'])


if __name__ == '__main__':
    unittest.main()
