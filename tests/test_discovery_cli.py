"""Offline CLI integration for source discovery and human USD watch settings."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from arcteryx_prices.__main__ import main
from arcteryx_prices.monitor import create_watch, initialize_monitor, update_watch
from arcteryx_prices.watch_discovery import add_discovered_watches


URL = 'https://outlet.arcteryx.com/us/en/shop/mens/synthetic-shell-1234'
STAMP = '2026-10-27T18:00:00Z'


def discovery():
    variants = [{'sku': f'SYNTHETIC-{color}-{size}', 'color': color, 'size': size,
                 'price_minor': 28000, 'availability': 'out_of_stock' if size == 'XXL' else 'in_stock'}
                for color in ('Blue', 'Black') for size in ('XL', 'XXL')]
    return {'url': URL, 'source_url': URL, 'captured_at': STAMP, 'label': 'Synthetic shell',
            'market': 'US', 'currency': 'USD', 'variants': variants}


class DiscoveryCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.network = patch('arcteryx_prices.collector.build_opener',
                             side_effect=AssertionError('Offline test cannot open a network connection'))
        self.opener = self.network.start()
        self.addCleanup(self.network.stop)
        self.smtp_patch = patch('arcteryx_prices.notifications.smtplib.SMTP_SSL',
                                side_effect=AssertionError('Offline test cannot send mail'))
        self.smtp = self.smtp_patch.start()
        self.addCleanup(self.smtp_patch.stop)

    def tearDown(self):
        self.opener.assert_not_called()
        self.smtp.assert_not_called()

    def cli(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['arcteryx_prices', '--db', str(self.path), *arguments]), \
                redirect_stdout(output), redirect_stderr(error):
            try:
                main()
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def watch(self):
        with closing(initialize_monitor(self.path)) as connection:
            return create_watch(connection, {'url': URL, 'sku': 'SYNTHETIC-Blue-XL',
                'color': 'Blue', 'size': 'XL', 'rule': 'new_low'})

    def rows(self):
        with closing(initialize_monitor(self.path)) as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM monitor_watches ORDER BY id')]

    def assert_no_prices(self):
        with closing(initialize_monitor(self.path)) as connection:
            for table in ('price_observations', 'monitor_checks', 'monitor_alerts'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_discover_requires_url_or_search_and_preserves_partial_search_results(self):
        report = {'query': 'gamma mx', 'queried_at': STAMP, 'complete': False,
                  'matches': [{'url': URL, 'source_url': 'https://outlet.arcteryx.com/us/en/c/mens',
                               'captured_at': STAMP}],
                  'sources': [{'url': 'https://arcteryx.com/us/en/c/mens', 'status': 'site_blocked'}]}
        with patch('arcteryx_prices.search.search_products', return_value=report) as search, \
                patch('arcteryx_prices.monitor.discover_for_monitor', return_value=discovery()) as direct:
            for args in (('discover',), ('discover', URL, '--search', 'gamma mx')):
                code, result, error = self.cli(*args)
                self.assertEqual(code, 2)
                self.assertIsNone(result)
                self.assertIn('exactly one', error)
            search.assert_not_called()
            direct.assert_not_called()
            code, result, error = self.cli('discover', '--search', 'gamma mx')
            self.assertEqual((code, result, error), (1, report, ''))
            self.assertEqual(search.call_args.args[1], 'gamma mx')
            direct.assert_not_called()
            search.return_value = report | {'complete': True}
            self.assertEqual(self.cli('discover', '--search', 'gamma mx')[0], 0)
            code, result, error = self.cli('discover', URL)
            self.assertEqual((code, result['captured_at'], error), (0, STAMP, ''))
            direct.assert_called_once()
        self.assert_no_prices()

    def test_watch_update_accepts_dollars_without_json_and_can_clear_target(self):
        watch = self.watch()
        for target, cents in (('280', 28000), ('279.95', 27995), ('none', None)):
            with self.subTest(target=target):
                code, result, error = self.cli('watch-update', str(watch['id']),
                    '--rule', 'available_or_target', '--target-price', target)
                self.assertEqual((code, error), (0, ''))
                self.assertEqual(result['rule'], 'available_or_target')
                self.assertEqual(result['target_price_minor'], cents)
                self.assertEqual(result['target_price'], f'{cents // 100}.{cents % 100:02d}' if cents else None)
                self.assertEqual(self.rows()[0]['target_price_minor'], cents)
        self.assert_no_prices()

    def test_invalid_or_duplicate_target_input_keeps_watch_unchanged(self):
        watch = self.watch()
        before = self.rows()
        for value in ('-1', '0', '2.999', 'NaN', 'Infinity', '$280', '1e3'):
            with self.subTest(value=value):
                code, _, _ = self.cli('watch-update', str(watch['id']), '--target-price=' + value)
                self.assertEqual(code, 2)
                self.assertEqual(self.rows(), before)
        config = Path(self.temp.name) / 'settings.json'
        for settings, extra in (({'target_price': '200'}, ['--target-price', '280']),
                                ({'target_price_minor': 20000}, ['--target-price', '280']),
                                ({'rule': 'new_low'}, ['--rule', 'available_or_target'])):
            config.write_text(json.dumps(settings), encoding='utf-8')
            code, _, error = self.cli('watch-update', str(watch['id']), str(config), *extra)
            self.assertEqual(code, 2)
            self.assertIn('not both', error)
            self.assertEqual(self.rows(), before)
        self.assertEqual(self.cli('watch-update', str(watch['id']))[0], 2)
        self.assert_no_prices()

    def test_watch_add_accepts_usd_option_and_rejects_ambiguous_file(self):
        config = Path(self.temp.name) / 'watch.json'
        payload = {'url': URL, 'sku': 'SYNTHETIC-Blue-XL', 'color': 'Blue', 'size': 'XL', 'rule': 'target'}
        config.write_text(json.dumps(payload), encoding='utf-8')
        code, result, _ = self.cli('watch-add', str(config), '--target-price', '279.95')
        self.assertEqual((code, result['target_price_minor']), (0, 27995))
        config.write_text(json.dumps(payload | {'sku': 'SYNTHETIC-Black-XL', 'target_price': 280}), encoding='utf-8')
        self.assertEqual(self.cli('watch-add', str(config), '--target-price', '279.95')[0], 2)
        self.assertEqual(len(self.rows()), 1)
        self.assert_no_prices()

    def test_watch_discover_adds_four_exact_variants_including_out_of_stock(self):
        with patch('arcteryx_prices.watch_discovery.discover_for_monitor', return_value=discovery()) as fetch:
            code, result, error = self.cli('watch-discover', URL, '--sizes', 'XL', 'XXL')
            self.assertEqual((code, result['created'], result['selected_variants'], error), (0, 4, 4, ''))
            self.assertEqual((result['source_url'], result['queried_at']), (URL, STAMP))
            self.assertEqual(result['colors'], ['Black', 'Blue'])
            self.assertEqual(result['unmatched_sizes'], [])
            self.assertEqual(result['unmatched_colors'], [])
            self.assertEqual(result['missing_combinations'], [])
            self.assertEqual({item['sku'] for item in result['variants']},
                             {item['sku'] for item in discovery()['variants']})
            fetch.assert_called_once()
        rows = self.rows()
        self.assertEqual({row['sku'] for row in rows}, {v['sku'] for v in discovery()['variants']})
        self.assertTrue(all(row['rule'] == 'available_or_target' and row['interval_minutes'] == 15
                            and row['target_price_minor'] is None for row in rows))
        self.assert_no_prices()

    def test_missing_color_size_combinations_are_visible_without_invented_skus(self):
        partial = discovery()
        partial['variants'] = [partial['variants'][0], partial['variants'][3]]
        with closing(initialize_monitor(self.path)) as connection, \
                patch('arcteryx_prices.watch_discovery.discover_for_monitor', return_value=partial):
            result = add_discovered_watches(connection, URL, sizes=['XL', 'XXL'])
        self.assertEqual(result['created'], 2)
        self.assertEqual({(item['color'].casefold(), item['size'].casefold())
                          for item in result['missing_combinations']}, {('blue', 'xxl'), ('black', 'xl')})
        self.assertTrue(all(set(item) == {'color', 'size'} for item in result['missing_combinations']))
        self.assertEqual({item['sku'] for item in result['variants']},
                         {'SYNTHETIC-Blue-XL', 'SYNTHETIC-Black-XXL'})
        self.assertEqual({row['sku'] for row in self.rows()}, {'SYNTHETIC-Blue-XL', 'SYNTHETIC-Black-XXL'})
        self.assert_no_prices()

    def test_repeated_discovery_preserves_existing_settings_and_history_identity(self):
        with closing(initialize_monitor(self.path)) as connection, \
                patch('arcteryx_prices.watch_discovery.discover_for_monitor', return_value=discovery()):
            first = add_discovered_watches(connection, URL, sizes=['XL', 'XXL'])
            update_watch(connection, first['watch_ids'][0], {'enabled': False,
                'rule': 'target', 'target_price': '200', 'interval_minutes': 60})
            before = self.rows()
            again = add_discovered_watches(connection, URL, sizes=['XL', 'XXL'], target_price='280')
            self.assertEqual((again['created'], again['skipped']), (0, 4))
            self.assertEqual(again['watch_ids'], first['watch_ids'])
            self.assertEqual(self.rows(), before)
        self.assert_no_prices()

    def test_exact_filters_report_unmatched_names_without_guessing_variants(self):
        with closing(initialize_monitor(self.path)) as connection, \
                patch('arcteryx_prices.watch_discovery.discover_for_monitor', return_value=discovery()):
            result = add_discovered_watches(connection, URL, sizes=[' xl ', 'XXXL'], colors=['BLUE', 'Green'])
            self.assertEqual(result['created'], 1)
            self.assertEqual(result['unmatched_sizes'], ['xxxl'])
            self.assertEqual(result['unmatched_colors'], ['green'])
            self.assertEqual(self.rows()[0]['sku'], 'SYNTHETIC-Blue-XL')
            with self.assertRaisesRegex(ValueError, 'No verified variants'):
                add_discovered_watches(connection, URL, sizes=['XXXL'])
            self.assertEqual(len(self.rows()), 1)
        self.assert_no_prices()

    def test_invalid_discovery_settings_fail_before_fetching(self):
        cases = ({'sizes': []}, {'sizes': ['']}, {'sizes': ['XL'], 'colors': []},
                 {'sizes': ['XL'], 'target_price': '2.999'}, {'sizes': ['XL'], 'rule': 'target'},
                 {'sizes': ['XL'], 'interval_minutes': 14}, {'sizes': ['XL'], 'rule': 'bad'})
        with closing(initialize_monitor(self.path)) as connection, \
                patch('arcteryx_prices.watch_discovery.discover_for_monitor') as fetch:
            for settings in cases:
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    add_discovered_watches(connection, URL, **settings)
            fetch.assert_not_called()
            self.assertEqual(self.rows(), [])
        self.assert_no_prices()


if __name__ == '__main__':
    unittest.main()
