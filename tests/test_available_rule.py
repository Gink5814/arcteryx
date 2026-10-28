"""Availability and target conditions have independent confirmed alert edges."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from arcteryx_prices.database import canonical_time, initialize
from arcteryx_prices.monitor import (
    check_watch, create_watch, initialize_monitor, parse_target_price, update_watch,
)


BASE = datetime(2026, 10, 28, 0, tzinfo=timezone.utc)
URL = 'https://outlet.arcteryx.com/us/en/shop/mens/synthetic-hoody-1234'


class AvailabilityRuleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize_monitor(self.path)
        self.watch = create_watch(self.db, {'url': URL, 'sku': 'SYNTHETIC-XXL', 'color': 'Black',
            'size': 'XXL', 'rule': 'available_or_target', 'target_price': None})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def check(self, price=28000, availability='in_stock', hour=0, confirmation=None):
        stamp = canonical_time(BASE + timedelta(hours=hour))
        quote = {'price_minor': price, 'availability': availability, 'observed_at': stamp,
                 'captured_at': stamp, 'source_url': URL, 'evidence_sha256': 'a' * 64}
        calls = []
        def fetch(watch):
            calls.append(watch)
            return quote | (confirmation or {}) if len(calls) > 1 else quote
        return check_watch(self.db, self.watch['id'], fetcher=fetch, now=stamp), calls

    def test_first_in_stock_check_alerts_then_restock_rearms_without_spam(self):
        first, calls = self.check()
        self.assertEqual(len(calls), 2)
        self.assertEqual(first['alert']['trigger_reason'], 'stock')
        self.assertEqual(first['alert']['availability'], 'in_stock')
        again, calls = self.check(hour=1)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(again['alert'])
        absent, _ = self.check(availability='out_of_stock', hour=2)
        self.assertIsNone(absent['alert'])
        restock, calls = self.check(hour=3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(restock['alert']['trigger_reason'], 'stock')

    def test_first_out_of_stock_waits_until_available_with_no_target(self):
        absent, calls = self.check(availability='out_of_stock')
        self.assertEqual(len(calls), 1)
        self.assertIsNone(absent['alert'])
        available, _ = self.check(hour=1)
        self.assertEqual(available['alert']['trigger_reason'], 'stock')

    def test_target_and_stock_trigger_independently_even_when_other_condition_stays_true(self):
        update_watch(self.db, self.watch['id'], {'target_price': '280.00'})
        low_unavailable, calls = self.check(availability='out_of_stock')
        self.assertEqual(len(calls), 2)
        self.assertEqual(low_unavailable['alert']['trigger_reason'], 'target')
        self.assertEqual(low_unavailable['alert']['availability'], 'out_of_stock')
        self.assertIsNone(low_unavailable['metrics']['observed_all_time_min'])
        restock, _ = self.check(hour=1)
        self.assertEqual(restock['alert']['trigger_reason'], 'stock')
        expensive, _ = self.check(price=30000, hour=2)
        self.assertIsNone(expensive['alert'])
        drop, _ = self.check(price=27999, hour=3)
        self.assertEqual(drop['alert']['trigger_reason'], 'target')
        repeat, calls = self.check(price=27000, hour=4)
        self.assertIsNone(repeat['alert'])
        self.assertEqual(len(calls), 1)

    def test_first_both_conditions_emit_one_combined_reason(self):
        update_watch(self.db, self.watch['id'], {'target_price': 280})
        result, calls = self.check()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['alert']['trigger_reason'], 'stock_and_target')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_alerts').fetchone()[0], 1)

    def test_failed_confirmation_does_not_consume_condition_or_invent_price(self):
        changed, _ = self.check(confirmation={'availability': 'out_of_stock'})
        self.assertEqual(changed['check_status'], 'verification_changed')
        self.assertIsNone(changed['alert'])
        self.assertEqual(changed['condition_mask'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 0)
        valid, calls = self.check(hour=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(valid['alert']['trigger_reason'], 'stock')

    def test_unknown_stock_does_not_rearm_an_existing_stock_condition(self):
        self.check()
        unknown, _ = self.check(availability='unknown', hour=1)
        self.assertIsNone(unknown['alert'])
        self.assertEqual(unknown['condition_mask'], 1)
        recovered, calls = self.check(hour=2)
        self.assertIsNone(recovered['alert'])
        self.assertEqual(len(calls), 1)

    def test_dollar_settings_reset_conditions_only_when_the_setting_changes(self):
        first, _ = self.check()
        unchanged = update_watch(self.db, self.watch['id'], {'rule': 'available_or_target', 'target_price': None})
        self.assertEqual(unchanged['condition_mask'], first['condition_mask'])
        changed = update_watch(self.db, self.watch['id'], {'target_price': '279.99'})
        self.assertEqual(changed['target_price_minor'], 27999)
        self.assertEqual(changed['target_price'], '279.99')
        self.assertEqual(changed['condition_mask'], 0)
        with self.assertRaisesRegex(ValueError, 'not both'):
            update_watch(self.db, self.watch['id'], {'target_price': '280', 'target_price_minor': 28000})


class DollarInputTest(unittest.TestCase):
    def test_exact_cents_and_upper_boundary(self):
        for value, cents in [('280', 28000), (280, 28000), ('279.99', 27999), (279.99, 27999),
                             ('0.01', 1), ('92233720368547758.07', 2**63 - 1), (None, None)]:
            with self.subTest(value=value):
                self.assertEqual(parse_target_price(value), cents)

    def test_ambiguous_rounding_nonfinite_and_invalid_values_are_rejected(self):
        for value in (True, False, 0, -1, '0', '-1', '$280', '1e2', '280.001', '280.000',
                      float('nan'), float('inf'), {}, [], '92233720368547758.08'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_target_price(value)


class AvailabilityMigrationTest(unittest.TestCase):
    def test_old_rule_check_and_missing_columns_upgrade_with_existing_alerts(self):
        schema = (Path(__file__).resolve().parents[1] / 'db' / 'monitor.sql').read_text()
        schema = schema.replace(",'available_or_target'", '')
        schema = '\n'.join(line for line in schema.splitlines()
                           if not any(marker in line for marker in (
                               'condition_mask INTEGER', "availability TEXT NOT NULL DEFAULT 'in_stock'", 'trigger_reason TEXT')))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.sqlite3'
            with closing(initialize(path)) as db:
                db.executescript(schema)
                watch = create_watch(db, {'url': URL, 'sku': 'SYNTHETIC-XXL', 'color': 'Black', 'size': 'XXL',
                    'rule': 'target', 'target_price_minor': 28000})
                with db:
                    db.execute('''INSERT INTO monitor_alerts
                        (watch_id,event_number,created_at,price_minor,currency,rule,url,label,evidence_sha256)
                        VALUES (?,1,?,28000,'USD','target',?,'Legacy alert',?)''',
                        (watch['id'], canonical_time(BASE), URL, 'b' * 64))
            with closing(initialize_monitor(path)) as db:
                alert = dict(db.execute('SELECT * FROM monitor_alerts').fetchone())
                self.assertEqual(alert['label'], 'Legacy alert')
                self.assertEqual(alert['availability'], 'in_stock')
                self.assertIsNone(alert['trigger_reason'])
                changed = update_watch(db, watch['id'], {'rule': 'available_or_target', 'target_price': None})
                self.assertEqual(changed['id'], watch['id'])
                self.assertEqual(changed['condition_mask'], 0)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                with self.assertRaises(sqlite3.IntegrityError), db:
                    db.execute("UPDATE monitor_watches SET rule='invalid'")
            with closing(initialize_monitor(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_alerts').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT rule FROM monitor_watches').fetchone()[0], 'available_or_target')


if __name__ == '__main__':
    unittest.main()
