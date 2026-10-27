"""Synthetic fixtures test calculations; none are real Arc'teryx prices."""

import tempfile
import unittest
from pathlib import Path

from arcteryx_prices import import_observations, initialize, metrics


def observation(price, observed='2026-10-26T12:00:00Z', **changes):
    record = {
        'product_key': 'synthetic-shell-2026', 'name': 'SYNTHETIC TEST PRODUCT',
        'model_code': 'TEST-001', 'season': '2026', 'variant_key': 'black-m',
        'color': 'Black', 'size': 'M', 'retailer_key': 'test-store',
        'retailer_name': 'Synthetic Store', 'offer_key': 'test-offer-m',
        'market': 'US', 'currency': 'USD', 'observed_at': observed,
        'captured_at': observed, 'price_minor': price, 'availability': 'in_stock',
        'product_condition': 'new', 'price_type': 'public',
        'source_type': 'manual', 'source_url': 'https://example.com/synthetic-fixture',
    }
    return record | changes


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = initialize(Path(self.temporary.name) / 'prices.sqlite3')

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def insert(self, *records):
        return import_observations(self.connection, {'observations': list(records)})

    def query(self, **overrides):
        args = {'product_key': 'synthetic-shell-2026', 'variant_key': 'black-m',
                'market': 'US', 'currency': 'USD', 'as_of': '2026-10-26T23:59:59Z'}
        return metrics(self.connection, **(args | overrides))

    def test_empty_history_has_no_invented_prices(self):
        result = self.query()
        self.assertIsNone(result['observed_all_time_min'])
        self.assertIsNone(result['lowest_365d'])
        self.assertIsNone(result['average_daily_low_90d']['mean_minor'])
        self.assertEqual(result['average_daily_low_90d']['missing_days'], 90)
        self.assertIsNone(result['history']['first_observed_at'])

    def test_average_uses_daily_cross_store_minimum_not_poll_frequency(self):
        self.insert(
            observation(10000, '2026-10-24T12:00:00Z'),
            observation(10000, '2026-10-24T13:00:00Z'),
            observation(10000, '2026-10-24T14:00:00Z'),
            observation(9000, '2026-10-24T15:00:00Z', retailer_key='test-store-2',
                        retailer_name='Synthetic Store 2'),
            observation(20000, '2026-10-26T12:00:00Z'),
        )
        result = self.query()
        average = result['average_daily_low_90d']
        self.assertEqual(average['mean_minor'], '14500.000000')
        self.assertEqual(average['observed_days'], 2)
        self.assertEqual(average['missing_days'], 88)
        self.assertEqual(average['observation_count'], 5)
        self.assertEqual(average['daily_minima'], [
            {'day': '2026-10-24', 'price_minor': 9000},
            {'day': '2026-10-26', 'price_minor': 20000},
        ])
        self.assertEqual(result['observed_all_time_min']['retailer_key'], 'test-store-2')
        self.assertFalse(average['complete_day_coverage'])

    def test_window_edges_and_future_records(self):
        self.insert(
            observation(100, '2025-10-26T23:59:59Z'),  # Before 365-calendar-date window.
            observation(200, '2025-10-27T00:00:00Z'),  # First date included.
            observation(300, '2026-07-28T23:59:59Z'),  # Before 90-date window.
            observation(400, '2026-07-29T00:00:00Z'),  # First 90-date included.
            observation(500, '2026-10-26T12:00:00Z'),
            observation(1, '2026-10-26T12:00:01Z'),    # After as_of, same date.
        )
        result = self.query(as_of='2026-10-26T12:00:00Z')
        self.assertEqual(result['observed_all_time_min']['price_minor'], 100)
        self.assertEqual(result['lowest_365d']['price_minor'], 200)
        self.assertEqual(result['average_daily_low_90d']['mean_minor'], '450.000000')
        self.assertEqual(result['history']['observation_count'], 5)

    def test_currency_variant_and_model_are_isolated(self):
        self.insert(
            observation(50000),
            observation(10, market='CA', currency='CAD'),
            observation(20, variant_key='black-s', size='S', offer_key='test-offer-s'),
            observation(30, product_key='synthetic-shell-2025', season='2025', offer_key='old-model'),
        )
        self.assertEqual(self.query()['lowest_365d']['price_minor'], 50000)
        self.assertEqual(self.query(market='CA', currency='CAD')['lowest_365d']['price_minor'], 10)
        with self.assertRaisesRegex(ValueError, 'market/currency'):
            self.query(market='CA', currency='USD')

    def test_retailer_and_offer_filters_apply_to_all_metrics(self):
        self.insert(
            observation(10000),
            observation(5000, retailer_key='test-store-2', retailer_name='Synthetic Store 2'),
            observation(2000, offer_key='test-offer-second'),
        )
        self.assertEqual(self.query()['lowest_365d']['price_minor'], 2000)
        scoped = self.query(retailer_key='test-store', offer_key='test-offer-m')
        self.assertEqual(scoped['observed_all_time_min']['price_minor'], 10000)
        self.assertEqual(scoped['lowest_365d']['price_minor'], 10000)
        self.assertEqual(scoped['average_daily_low_90d']['mean_minor'], '10000.000000')
        with self.assertRaisesRegex(ValueError, 'requires retailer_key'):
            self.query(offer_key='test-offer-m')

    def test_only_in_stock_new_public_prices_qualify(self):
        self.insert(
            observation(50000),
            observation(10, '2026-10-25T01:00:00Z', availability='out_of_stock'),
            observation(20, '2026-10-25T02:00:00Z', availability='unknown'),
            observation(30, '2026-10-25T03:00:00Z', product_condition='used'),
            observation(40, '2026-10-25T04:00:00Z', product_condition='refurbished'),
            observation(50, '2026-10-25T05:00:00Z', price_type='member'),
            observation(60, '2026-10-25T06:00:00Z', price_type='coupon'),
            observation(70, '2026-10-25T07:00:00Z', price_type='bundle'),
        )
        result = self.query()
        self.assertEqual(result['observed_all_time_min']['price_minor'], 50000)
        self.assertEqual(result['history']['observation_count'], 1)

    def test_offsets_are_normalized_before_daily_grouping(self):
        self.insert(observation(100, '2026-10-25T23:00:00-06:00'))
        result = self.query()
        self.assertEqual(result['average_daily_low_90d']['daily_minima'][0]['day'], '2026-10-26')
        self.assertEqual(result['history']['first_observed_at'], '2026-10-26T05:00:00.000000Z')

    def test_duplicate_import_preserves_first_capture_and_conflicts_rollback(self):
        first = observation(20000)
        self.assertEqual(self.insert(first), {'inserted': 1, 'duplicates': 0})
        later = first | {'captured_at': '2026-10-27T12:00:00Z'}
        self.assertEqual(self.insert(later), {'inserted': 0, 'duplicates': 1})
        with self.assertRaisesRegex(ValueError, 'conflicting price observation'):
            self.insert(observation(100, '2026-10-25T12:00:00Z'), first | {'price_minor': 1})
        self.assertEqual(self.query()['history']['observation_count'], 1)
        self.assertEqual(self.query()['observed_all_time_min']['captured_at'], '2026-10-26T12:00:00.000000Z')

    def test_reused_identity_cannot_silently_change_size_or_season(self):
        self.insert(observation(10000))
        for changes in ({'size': 'S'}, {'season': '2025'}, {'name': 'Different Shell'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'conflicting'):
                self.insert(observation(5000, '2026-10-25T12:00:00Z', **changes))
        self.assertEqual(self.query()['history']['observation_count'], 1)

    def test_invalid_amount_time_source_and_market_are_rejected_atomically(self):
        for changes in (
            {'price_minor': 1.5}, {'price_minor': True}, {'price_minor': 0},
            {'price_minor': 2**63},
            {'observed_at': '2026-10-26T12:00:00'},
            {'captured_at': '2026-10-25T12:00:00Z'},
            {'source_url': 'https://user:secret@example.com/item'},
            {'market': 'US', 'currency': 'CAD'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.insert(observation(10000, **changes))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM products').fetchone()[0], 0)

    def test_rounding_preserves_exact_sum_and_number_of_days(self):
        self.insert(observation(100, '2026-10-24T12:00:00Z'),
                    observation(101, '2026-10-25T12:00:00Z'),
                    observation(101, '2026-10-26T12:00:00Z'))
        result = self.query()['average_daily_low_90d']
        self.assertEqual(result['sum_daily_min_minor'], 302)
        self.assertEqual(result['observed_days'], 3)
        self.assertEqual(result['mean_minor'], '100.666667')
        self.assertEqual(result['display_minor'], 101)


if __name__ == '__main__':
    unittest.main()
