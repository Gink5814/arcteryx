"""Existing SQLite databases retain their facts when the interval minimum drops."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from arcteryx_prices.database import canonical_time, initialize
from arcteryx_prices.history import initialize_history
from arcteryx_prices.migrations import migrate_watch_interval
from arcteryx_prices.monitor import check_watch, create_watch, initialize_monitor, update_watch


ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-migration-shell'


class IntervalMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize(self.path)
        old_schema = (ROOT / 'db/monitor.sql').read_text().replace('BETWEEN 15 AND 1440', 'BETWEEN 30 AND 1440')
        self.db.executescript(old_schema)
        initialize_history(self.db)
        self.db.executescript((ROOT / 'db/notifications.sql').read_text())
        self.watch = create_watch(self.db, {'url': URL, 'sku': 'SYNTHETIC-M', 'color': 'Black',
            'size': 'M', 'label': 'Synthetic legacy watch', 'rule': 'target', 'target_price_minor': 50000})
        quote = {'price_minor': 45000, 'availability': 'in_stock',
                 'observed_at': canonical_time(BASE), 'captured_at': canonical_time(BASE),
                 'source_url': URL, 'evidence_sha256': 'a' * 64}
        check_watch(self.db, self.watch['id'], fetcher=lambda watch: quote, now=BASE)
        self.db.executescript('''CREATE TABLE interval_audit(watch_id INTEGER, interval_minutes INTEGER);
            CREATE INDEX custom_watch_label ON monitor_watches(label);
            CREATE TRIGGER custom_watch_interval AFTER UPDATE OF interval_minutes ON monitor_watches
            BEGIN INSERT INTO interval_audit VALUES(NEW.id,NEW.interval_minutes); END;
            CREATE VIEW current_watch_names AS SELECT id,label FROM monitor_watches;''')
        with self.db:
            self.db.execute('''INSERT INTO notification_deliveries
                (event_key,recipient,kind,watch_id,created_at) VALUES ('price:1','test@example.com','price',?,?)''',
                (self.watch['id'], canonical_time(BASE)))
            self.db.execute('''INSERT INTO historical_evidence
                (evidence_key,source_url,observed_at,captured_at,payload_json,observation_id,watch_id)
                VALUES ('synthetic',?,?,?,'{}',1,?)''',
                (URL, canonical_time(BASE), canonical_time(BASE), self.watch['id']))
            self.db.execute('''UPDATE monitor_watches SET lease_token='synthetic-lease',lease_until=?,
                retry_not_before=? WHERE id=?''',
                (canonical_time(BASE + timedelta(minutes=10)), canonical_time(BASE + timedelta(hours=1)), self.watch['id']))
            self.db.execute('''INSERT INTO monitor_host_cooldowns VALUES ('arcteryx.com',?,429,?)''',
                            (canonical_time(BASE + timedelta(hours=1)), canonical_time(BASE)))

    def tearDown(self):
        self.db.close()

    def facts(self):
        tables = ('monitor_watches', 'monitor_checks', 'monitor_alerts', 'price_observations',
                  'historical_evidence', 'notification_deliveries', 'monitor_host_cooldowns', 'interval_audit')
        return {table: [tuple(row) for row in self.db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in tables}

    def test_populated_legacy_database_preserves_facts_foreign_keys_indexes_triggers_and_views(self):
        before = self.facts()
        self.db.close()
        self.db = initialize_monitor(self.path)
        self.assertEqual(before, self.facts())
        self.assertEqual(self.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT label FROM current_watch_names').fetchone()[0], self.watch['label'])
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_due'").fetchone())
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='custom_watch_label'").fetchone())
        changed = update_watch(self.db, self.watch['id'], {'interval_minutes': 15})
        self.assertEqual(changed['interval_minutes'], 15)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM interval_audit').fetchone()), (self.watch['id'], 15))
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute('UPDATE monitor_watches SET interval_minutes=14')
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("INSERT INTO notification_deliveries VALUES ('bad','test@example.com','price',999,?,'pending',0,NULL,NULL,NULL)",
                            (canonical_time(BASE),))
        migrated = self.facts()
        self.db.close()
        self.db = initialize_monitor(self.path)
        self.assertEqual(migrated, self.facts(), 'a second startup must not rewrite facts')

    def test_failed_rebuild_rolls_back_and_restores_connection_pragmas(self):
        before = self.facts()
        original_schema = self.db.execute("SELECT sql FROM sqlite_master WHERE name='monitor_watches'").fetchone()[0]
        def deny_index_creation(action, name, *unused):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX and name == 'custom_watch_label' else sqlite3.SQLITE_OK
        self.db.set_authorizer(deny_index_creation)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_watch_interval(self.db)
        finally:
            self.db.set_authorizer(None)
        self.assertEqual(before, self.facts())
        self.assertEqual(original_schema, self.db.execute("SELECT sql FROM sqlite_master WHERE name='monitor_watches'").fetchone()[0])
        self.assertEqual(self.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(self.db.execute('PRAGMA legacy_alter_table').fetchone()[0], 0)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='_monitor_watches_interval_migration'").fetchone())

    def test_concurrent_initializers_finish_one_idempotent_upgrade(self):
        before = self.facts()
        def initialize_worker(unused):
            connection = initialize_monitor(self.path)
            try:
                return connection.execute('PRAGMA foreign_key_check').fetchall()
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(initialize_worker, range(2))), [[], []])
        self.assertEqual(before, self.facts())
        self.assertEqual(update_watch(self.db, self.watch['id'], {'interval_minutes': 15})['interval_minutes'], 15)


if __name__ == '__main__':
    unittest.main()
