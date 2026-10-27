"""Portable watch imports use isolated databases and never collect prices."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from arcteryx_prices.__main__ import main
from arcteryx_prices.monitor import (
    check_watch, create_watch, import_watches, initialize_monitor, update_watch,
)


URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
WATCH = {'url': URL, 'sku': 'SYNTHETIC-XL', 'color': 'Black', 'size': 'XL',
         'label': 'Synthetic shell', 'rule': 'new_low', 'interval_minutes': 15}


class WatchImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize_monitor(self.path)
        self.network = patch('arcteryx_prices.collector.build_opener', side_effect=AssertionError('No network'))
        self.opener = self.network.start()

    def tearDown(self):
        self.opener.assert_not_called()
        self.network.stop()
        self.db.close()
        self.temp.cleanup()

    def watches(self):
        return [tuple(row) for row in self.db.execute('SELECT * FROM monitor_watches ORDER BY id')]

    def test_import_and_repeat_preserve_settings_and_create_no_observations(self):
        other = WATCH | {'sku': 'SYNTHETIC-XXL', 'size': 'XXL'}
        first = import_watches(self.db, [WATCH, other])
        self.assertEqual(first, {'created': 2, 'skipped': 0, 'watch_ids': [1, 2]})
        update_watch(self.db, 1, {'rule': 'target', 'target_price_minor': 30000, 'interval_minutes': 60, 'enabled': False})
        before = self.watches()
        self.db.close()
        self.db = initialize_monitor(self.path)
        second = import_watches(self.db, [WATCH | {'label': 'Other label'}, other])
        self.assertEqual(second, {'created': 0, 'skipped': 2, 'watch_ids': [1, 2]})
        self.assertEqual(self.watches(), before)
        for table in ('price_observations', 'monitor_checks', 'monitor_alerts'):
            self.assertEqual(self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_duplicate_in_one_batch_and_normalized_url_are_skipped(self):
        normalized = WATCH | {'url': URL.replace('arcteryx.com', 'ARCTERYX.COM:443') + '#details'}
        result = import_watches(self.db, [WATCH, normalized])
        self.assertEqual(result, {'created': 1, 'skipped': 1, 'watch_ids': [1, 1]})

    def test_invalid_later_item_rolls_back_all_new_watches(self):
        existing = create_watch(self.db, WATCH)
        before = self.watches()
        for invalid in (None, WATCH | {'interval_minutes': 14}, WATCH | {'rule': 'invalid'},
                        WATCH | {'url': URL.replace('/us/en/', '/ca/en/')}, WATCH | {'extra': True}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'watch item 2:'):
                import_watches(self.db, [WATCH | {'sku': 'NEW-SKU'}, invalid])
            self.assertEqual(self.watches(), before)
        self.assertEqual(existing['id'], 1)

    def test_conflicting_identity_rolls_back_even_within_same_batch(self):
        for changed in ({'color': 'Blue'}, {'size': 'XXL'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'different variant identity'):
                import_watches(self.db, [WATCH, WATCH | changed])
            self.assertEqual(self.watches(), [])

    def test_existing_history_is_not_rewritten_and_conflict_preserves_it(self):
        watch = create_watch(self.db, WATCH)
        stamp = '2026-10-27T12:00:00Z'
        quote = {'source_url': URL, 'price_minor': 40000, 'availability': 'in_stock',
                 'observed_at': stamp, 'captured_at': stamp, 'evidence_sha256': 'a' * 64,
                 'sku': WATCH['sku'], 'color': WATCH['color'], 'size': WATCH['size'],
                 'market': 'US', 'currency': 'USD'}
        check_watch(self.db, watch['id'], fetcher=lambda _: quote, now=stamp)
        before = self.watches()
        observations = [tuple(row) for row in self.db.execute('SELECT * FROM price_observations')]
        self.assertEqual(import_watches(self.db, [WATCH])['skipped'], 1)
        with self.assertRaisesRegex(ValueError, 'different variant identity'):
            import_watches(self.db, [WATCH | {'sku': 'NEW-SKU'}, WATCH | {'color': 'Blue'}])
        self.assertEqual(self.watches(), before)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM price_observations')], observations)

    def test_cli_import_reports_summary_and_bad_top_level_is_rejected(self):
        file = Path(self.temp.name) / 'watches.json'
        file.write_text(json.dumps([WATCH]), encoding='utf-8')
        output = io.StringIO()
        with patch.object(sys, 'argv', ['arcteryx_prices', '--db', str(self.path), 'watch-import', str(file)]), \
                contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue()), {'created': 1, 'skipped': 0, 'watch_ids': [1]})
        for invalid in (WATCH, {'watches': [WATCH]}, None, 'not an array'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'array'):
                import_watches(self.db, invalid)
        self.assertEqual(import_watches(self.db, []), {'created': 0, 'skipped': 0, 'watch_ids': []})


if __name__ == '__main__':
    unittest.main()
