"""HTTP end-to-end checks on loopback with isolated DBs and synthetic quotes.

The server has no background worker in these tests, and no test uses a live
retailer, a real product price, or the repository's data/prices.sqlite3.
"""

import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from arcteryx_prices.collector import CollectorError
from arcteryx_prices.server import MonitoringServer


PRODUCT_URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'


def synthetic_quote(watch):
    timestamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    return {key: watch[key] for key in ('sku', 'color', 'size', 'market', 'currency')} | {
        'price_minor': 42000,
        'availability': 'in_stock',
        'observed_at': timestamp,
        'captured_at': timestamp,
        'source_url': watch['url'],
        'evidence_sha256': hashlib.sha256(b'SYNTHETIC HTTP FIXTURE ONLY').hexdigest(),
    }


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / 'http-test.sqlite3'
        self.fetcher = Mock(side_effect=synthetic_quote)
        self.server = MonitoringServer(self.database_path, port=0, fetcher=self.fetcher)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True,
        )
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        status, state, _ = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.token = state['csrf_token']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method, path, payload=None, *, authenticated=True, headers=None, raw=None):
        request_headers = dict(headers or {})
        if method in {'POST', 'PATCH'}:
            request_headers.setdefault('Content-Type', 'application/json')
            request_headers.setdefault('Origin', self.origin)
            if authenticated:
                request_headers.setdefault('X-CSRF-Token', self.token)
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            client.request(method, path, body=body, headers=request_headers)
            response = client.getresponse()
            data = response.read()
            response_headers = dict(response.getheaders())
            value = json.loads(data) if response_headers.get('Content-Type', '').startswith('application/json') else data.decode()
            return response.status, value, response_headers
        finally:
            client.close()

    def payload(self, **changes):
        return {
            'url': PRODUCT_URL, 'sku': 'SYNTHETIC-HTTP-SKU-M', 'color': 'Synthetic Black',
            'size': 'M', 'label': 'SYNTHETIC HTTP WATCH', 'rule': 'target',
            'target_price_minor': 50000, 'interval_minutes': 60,
        } | changes

    def create(self, **changes):
        status, watch, _ = self.request('POST', '/api/watches', self.payload(**changes))
        self.assertEqual(status, 201, watch)
        return watch

    def observation_count(self):
        with closing(sqlite3.connect(self.database_path)) as database:
            return database.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0]

    def test_create_check_metrics_alert_persist_and_pause_over_http(self):
        watch = self.create()
        self.fetcher.assert_not_called()
        self.assertEqual(self.observation_count(), 0)
        self.assertIsNone(watch['metrics']['observed_all_time_min'])
        status, checked, _ = self.request('POST', f"/api/watches/{watch['id']}/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(checked['check_status'], 'ok')
        self.assertEqual(self.fetcher.call_count, 2)  # candidate and confirmation
        self.assertEqual(checked['alert']['price_minor'], 42000)
        status, state, _ = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        current = state['watches'][0]
        self.assertEqual(current['current_price_minor'], 42000)
        self.assertEqual(current['availability'], 'in_stock')
        self.assertFalse(current['stale'])
        self.assertEqual(current['metrics']['observed_all_time_min']['price_minor'], 42000)
        self.assertEqual(current['metrics']['lowest_365d']['price_minor'], 42000)
        self.assertEqual(current['metrics']['average_daily_low_90d']['observed_days'], 1)
        self.assertEqual(current['metrics']['average_daily_low_90d']['mean_minor'], '42000.000000')
        self.assertEqual(len(state['alerts']), 1)
        self.assertEqual(state['alerts'][0]['market'], 'US')
        self.assertEqual(state['alerts'][0]['currency'], 'USD')
        self.assertEqual(state['notifications'], 'in_app')
        self.request('POST', f"/api/watches/{watch['id']}/check", {})
        self.assertEqual(self.fetcher.call_count, 3)  # continuing sale is not re-alerted
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(len(state['alerts']), 1)
        status, paused, _ = self.request('PATCH', f"/api/watches/{watch['id']}", {'enabled': False})
        self.assertEqual(status, 200)
        self.assertEqual(paused['status'], 'paused')
        self.assertIsNone(paused['current_price_minor'])
        status, skipped, _ = self.request('POST', f"/api/watches/{watch['id']}/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(skipped['check_status'], 'paused')
        self.assertEqual(self.fetcher.call_count, 3)

    def test_historical_reference_api_is_read_only_and_separate_from_watch_metrics(self):
        from arcteryx_prices.history import import_evidence
        from arcteryx_prices.monitor import initialize_monitor
        record = {
            'evidence_key': 'SYNTHETIC-HTTP-REPORT',
            'source_url': 'https://slickdeals.net/f/123-synthetic-http-fixture',
            'publisher': 'SYNTHETIC publisher', 'source_kind': 'deal_report',
            'observed_at': '2025-09-01', 'date_precision': 'day',
            'captured_at': '2026-01-01T00:00:00Z', 'price_minor': 12345,
            'market': 'US', 'currency': 'USD', 'retailer_key': 'rei',
            'retailer_name': 'REI Co-op', 'product_name': 'SYNTHETIC product',
            'availability': 'unknown', 'product_condition': 'unknown', 'price_type': 'public',
            'excerpt': 'Synthetic fixture, not a retail price.', 'body_sha256': 'a' * 64,
        }
        with closing(initialize_monitor(self.database_path)) as database, database:
            import_evidence(database, {'schema_version': 1, 'evidence': [record]})
        status, state, _ = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['historical_evidence'][0]['observed_at'], '2025-09-01')
        self.assertFalse(state['historical_evidence'][0]['included_in_metrics'])
        self.assertTrue(any(item['key'] == 'rei' for item in state['supported_retailers']))
        status, evidence, _ = self.request('GET', '/api/history-evidence')
        self.assertEqual(status, 200)
        self.assertEqual(evidence['evidence'], state['historical_evidence'])
        self.assertEqual(self.request('POST', '/api/history-evidence', {})[0], 404)
        self.assertEqual(self.observation_count(), 0)
        self.fetcher.assert_not_called()

    def test_health_and_static_assets_are_served_with_security_headers(self):
        status, health, headers = self.request('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path, content_type in [('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css')]:
            with self.subTest(path=path):
                status, body, headers = self.request('GET', path)
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith(content_type))
                self.assertTrue(body)
        self.assertEqual(self.request('GET', '/not-found')[0], 404)
        self.fetcher.assert_not_called()

    def test_us_only_api_rejects_canadian_requests_and_hides_retained_legacy_watch(self):
        ca_url = PRODUCT_URL.replace('/us/en/', '/ca/en/')
        self.assertEqual(self.request('POST', '/api/watches', self.payload(url=ca_url))[0], 400)
        with patch('arcteryx_prices.collector._read_page') as fetch:
            self.assertEqual(self.request('POST', '/api/discover', {'url': ca_url})[0], 422)
            fetch.assert_not_called()
        watch = self.create()
        with closing(sqlite3.connect(self.database_path)) as database, database:
            database.execute("UPDATE monitor_watches SET market='CA',currency='CAD',url=?", (ca_url,))
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(state['scope'], ['US/USD'])
        self.assertTrue(all(source['markets'] == ['US'] for source in state['supported_retailers']))
        self.assertEqual(state['watches'], [])
        self.assertEqual(self.request('PATCH', f"/api/watches/{watch['id']}", {'enabled': True})[0], 400)
        self.assertEqual(self.request('POST', f"/api/watches/{watch['id']}/check", {})[0], 400)
        self.fetcher.assert_not_called()
        with closing(sqlite3.connect(self.database_path)) as database:
            self.assertEqual(database.execute('SELECT COUNT(*),SUM(enabled) FROM monitor_watches').fetchone(), (1, 0))

    def test_host_origin_and_csrf_protection_reject_before_mutation(self):
        self.assertEqual(self.request('GET', '/api/state', headers={'Host': 'attacker.invalid'})[0], 403)
        for options in (
            {'authenticated': False},
            {'headers': {'X-CSRF-Token': 'incorrect-token'}},
            {'headers': {'Host': 'attacker.invalid'}},
            {'headers': {'Origin': 'https://attacker.invalid'}},
        ):
            with self.subTest(options=options):
                status, error, _ = self.request('POST', '/api/watches', self.payload(), **options)
                self.assertEqual(status, 403, error)
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(state['watches'], [])
        self.assertEqual(self.observation_count(), 0)
        self.fetcher.assert_not_called()
        self.create()  # the valid same-origin token still succeeds

    def test_invalid_url_and_unsupported_markets_never_fetch(self):
        for url in (
            'https://example.com/us/en/shop/mens/synthetic-shell',
            'http://arcteryx.com/us/en/shop/mens/synthetic-shell',
            'https://arcteryx.com.evil.invalid/us/en/shop/mens/synthetic-shell',
            'https://arcteryx.com/uk/en/shop/mens/synthetic-shell',
            'https://arcteryx.com/mx/en/shop/mens/synthetic-shell',
            'https://arcteryx.com/us/en/shop/mens/%2e%2e/secrets',
        ):
            with self.subTest(url=url):
                status, error, _ = self.request('POST', '/api/watches', self.payload(url=url))
                self.assertEqual(status, 400, error)
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(state['watches'], [])
        self.fetcher.assert_not_called()

    def test_missing_identity_invalid_json_and_check_payload_are_400(self):
        for field in ('url', 'sku', 'color', 'size', 'target_price_minor'):
            payload = self.payload()
            del payload[field]
            with self.subTest(field=field):
                status, error, _ = self.request('POST', '/api/watches', payload)
                self.assertEqual(status, 400, error)
        self.assertEqual(self.request('POST', '/api/watches', raw=b'{broken-json')[0], 400)
        self.assertEqual(self.request('POST', '/api/watches', [],)[0], 400)
        self.assertEqual(self.request('POST', '/api/watches', self.payload(), headers={'Content-Type':'text/plain'})[0], 400)
        watch = self.create()
        self.assertEqual(self.request('POST', f"/api/watches/{watch['id']}/check", {'unexpected': True})[0], 400)
        self.assertEqual(self.request('PATCH', f"/api/watches/{watch['id']}", {'url':PRODUCT_URL})[0], 400)
        self.fetcher.assert_not_called()

    def test_discovery_returns_variants_without_creating_history(self):
        discovered = {
            'url': PRODUCT_URL, 'market': 'US', 'currency': 'USD',
            'label': 'SYNTHETIC DISCOVERY FIXTURE',
            'variants': [{'sku':'SYNTHETIC-HTTP-SKU-M', 'color':'Synthetic Black',
                          'size':'M', 'price_minor':42000, 'availability':'in_stock'}],
        }
        with patch('arcteryx_prices.collector.discover_variants', return_value=discovered) as discover:
            status, result, _ = self.request('POST', '/api/discover', {'url':PRODUCT_URL})
            self.assertEqual(status, 200)
            self.assertEqual(result, discovered)
            discover.assert_called_once_with(PRODUCT_URL)
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(state['watches'], [])
        self.assertEqual(state['alerts'], [])
        self.assertEqual(self.observation_count(), 0)
        self.fetcher.assert_not_called()

    def test_discovery_errors_and_authentication_do_not_import_or_fetch(self):
        with patch('arcteryx_prices.collector.discover_variants') as discover:
            self.assertEqual(self.request('POST', '/api/discover', {'url':PRODUCT_URL}, authenticated=False)[0], 403)
            self.assertEqual(self.request('POST', '/api/discover', {})[0], 400)
            self.assertEqual(self.request('POST', '/api/discover', {'url':PRODUCT_URL,'market':'US'})[0], 400)
            discover.assert_not_called()
            discover.side_effect = CollectorError('no_verifiable_variants', 'SYNTHETIC unavailable variant data')
            status, failure, _ = self.request('POST', '/api/discover', {'url':PRODUCT_URL})
            self.assertEqual(status, 422)
            self.assertEqual(failure['status'], 'no_verifiable_variants')
        with patch('arcteryx_prices.collector._read_page') as read:
            status, failure, _ = self.request('POST', '/api/discover', {'url':'https://example.com/product'})
            self.assertEqual(status, 422)
            self.assertEqual(failure['status'], 'invalid_url')
            read.assert_not_called()
        self.assertEqual(self.observation_count(), 0)

    def test_discovery_rate_limit_is_shared_with_checks_but_not_other_retailers(self):
        deadline = datetime.now(timezone.utc) + timedelta(hours=2)
        failure = CollectorError('http_error', 'SYNTHETIC rate limit', http_status=429,
                                 retryable=True, retry_after=deadline)
        with patch('arcteryx_prices.collector.discover_variants', side_effect=failure) as discover:
            status, response, _ = self.request('POST', '/api/discover', {'url': PRODUCT_URL})
            self.assertEqual(status, 429)
            self.assertIsNotNone(response['retry_at'])
            status, response, _ = self.request('POST', '/api/discover', {'url': PRODUCT_URL})
            self.assertEqual(status, 429)
            self.assertEqual(response['status'], 'rate_limited')
            self.assertEqual(discover.call_count, 1)
        watch = self.create()
        status, result, _ = self.request('POST', f"/api/watches/{watch['id']}/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(result['check_status'], 'rate_limited')
        self.fetcher.assert_not_called()
        self.assertEqual(self.observation_count(), 0)
        rei = self.create(url='https://www.rei.com/product/123456/arcteryx-synthetic-shell')
        status, result, _ = self.request('POST', f"/api/watches/{rei['id']}/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(result['check_status'], 'ok')
        self.assertEqual(self.fetcher.call_count, 2)

    def test_collection_failure_is_visible_without_a_current_price_or_new_alert(self):
        watch = self.create()
        _, success, _ = self.request('POST', f"/api/watches/{watch['id']}/check", {})
        self.fetcher.side_effect = CollectorError('http_error', 'SYNTHETIC HTTP 403')
        status, failure, _ = self.request('POST', f"/api/watches/{watch['id']}/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(failure['status'], 'http_error')
        self.assertTrue(failure['stale'])
        self.assertIsNone(failure['current_price_minor'])
        self.assertEqual(failure['last_success_at'], success['last_success_at'])
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(state['watches'][0]['status'], 'http_error')
        self.assertEqual(len(state['alerts']), 1)
        self.assertEqual(self.observation_count(), 1)

    def test_concurrent_http_checks_share_a_database_lease(self):
        watch = self.create()
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        def blocking_quote(value):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError('Synthetic test release timed out')
            return synthetic_quote(value)
        self.fetcher.side_effect = blocking_quote
        first = threading.Thread(target=lambda: outcomes.append(
            self.request('POST', f"/api/watches/{watch['id']}/check", {})), daemon=True)
        first.start()
        try:
            self.assertTrue(entered.wait(timeout=3))
            status, busy, _ = self.request('POST', f"/api/watches/{watch['id']}/check", {})
            self.assertEqual(status, 200)
            self.assertEqual(busy['check_status'], 'busy')
            self.assertEqual(self.fetcher.call_count, 1)
        finally:
            release.set()
            first.join(timeout=4)
        self.assertFalse(first.is_alive())
        self.assertEqual(outcomes[0][0], 200)
        self.assertEqual(outcomes[0][1]['check_status'], 'ok')
        _, state, _ = self.request('GET', '/api/state')
        self.assertEqual(len(state['alerts']), 1)
        self.assertEqual(self.fetcher.call_count, 2)


if __name__ == '__main__':
    unittest.main()
