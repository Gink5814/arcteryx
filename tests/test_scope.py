"""CLI scope regression: only synthetic inputs and temporary databases."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcteryx_prices.__main__ import main
from arcteryx_prices.monitor import initialize_monitor
from test_database import observation


class ScopeCLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'synthetic.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def reject(self, *args):
        errors = io.StringIO()
        with patch('sys.argv', ['arcteryx_prices', '--db', str(self.db), *args]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors), \
                self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('Only US/USD', errors.getvalue())

    def test_mixed_market_raw_import_rejected_before_any_observation_is_written(self):
        source = Path(self.temp.name) / 'synthetic.json'
        source.write_text(json.dumps({'observations': [observation(100),
                         observation(200, market='CA', currency='CAD')]}))
        self.reject('import', str(source))
        database = initialize_monitor(self.db)
        try:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 0)
        finally:
            database.close()

    def test_canadian_metrics_are_not_exposed_by_the_us_product_cli(self):
        self.reject('metrics', '--product-key', 'synthetic', '--variant-key', 'synthetic',
                    '--market', 'CA', '--currency', 'CAD')


if __name__ == '__main__':
    unittest.main()
