"""Source outages stay visible even when most variant checks are skipped."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from arcteryx_prices.monitor import create_watch, import_watches, initialize_monitor
from arcteryx_prices.runner import run_once


BASE = '2026-10-27T12:00:00Z'
PRESET = Path(__file__).resolve().parents[1] / 'examples' / 'watches.gamma-mx-sidewinder.us.json'


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        connection = initialize_monitor(self.path)
        try:
            import_watches(connection, json.loads(PRESET.read_text(encoding='utf-8')))
        finally:
            connection.close()

    def test_blocked_preset_counts_one_request_and_previews_one_source_notice(self):
        def denied(request, **kwargs):
            raise HTTPError(request.full_url, 403, 'denied', {}, io.BytesIO(b''))
        with patch('arcteryx_prices.collector.build_opener') as opener, \
                patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
            opener.return_value.open.side_effect = denied
            first, code = run_once(self.path, dry_run=True, now=BASE)
            self.assertEqual(code, 1)
            self.assertEqual((first['checked'], first['skipped'], first['page_fetches']), (1, 19, 1))
            self.assertEqual((first['cache_hits'], first['confirmation_fetches']), (0, 0))
            self.assertTrue(first['all_sources_blocked'])
            self.assertEqual(len(first['unhealthy_watches']), 20)
            self.assertEqual(first['blocked_sources'][0]['affected_watches'], 20)
            self.assertIn('All price monitoring is unavailable', first['message'])
            self.assertIn('Dry run: no email is sent', first['message'])
            self.assertEqual(first['notifications']['would_send'], 1)
            again, code = run_once(self.path, dry_run=True, force=True, now='2026-10-27T12:15:00Z')
            self.assertEqual(code, 1)
            self.assertEqual((again['checked'], again['skipped'], again['page_fetches']), (0, 20, 0))
            self.assertTrue(again['all_sources_blocked'])
            self.assertEqual(again['blocked_sources'], first['blocked_sources'])
            opener.return_value.open.assert_called_once()
            smtp.assert_not_called()

    def test_working_other_source_is_not_reported_as_completely_blocked(self):
        connection = initialize_monitor(self.path)
        try:
            create_watch(connection, {'url': 'https://arcteryx.com/us/en/shop/mens/synthetic-shell',
                'sku': 'SYNTHETIC-XL', 'color': 'Black', 'size': 'XL', 'rule': 'new_low'})
        finally:
            connection.close()
        def fetch(watch):
            if watch['retailer_key'] == 'rei':
                from arcteryx_prices.collector import CollectorError
                raise CollectorError('site_blocked', 'Synthetic denial', http_status=403)
            return {key: watch[key] for key in ('sku', 'color', 'size', 'market', 'currency')} | {
                'price_minor': 10000, 'availability': 'in_stock', 'source_url': watch['url'],
                'observed_at': BASE, 'captured_at': BASE, 'evidence_sha256': 'a' * 64}
        result, code = run_once(self.path, dry_run=True, now=BASE, fetcher=fetch)
        self.assertEqual(code, 1)
        self.assertFalse(result['all_sources_blocked'])
        self.assertEqual((result['enabled_watches'], len(result['unhealthy_watches'])), (21, 20))
        self.assertEqual(result['blocked_sources'][0]['affected_watches'], 20)
        self.assertIsNone(result['page_fetches'])
        self.assertNotIn('All price monitoring is unavailable', result['message'])


if __name__ == '__main__':
    unittest.main()
