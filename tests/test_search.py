"""Official listing search uses synthetic HTML and never contacts retailers."""

from contextlib import closing
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from arcteryx_prices.collector import CollectorError, _read_page, discover_variants
from arcteryx_prices.monitor import initialize_monitor
from arcteryx_prices.search import (
    _CatalogRedirectHandler, _listing_matches, _query_tokens, _read_catalog_page,
    search_products, validate_catalog_url,
)


BASE = '2026-10-27T12:00:00Z'
CATALOG = 'https://outlet.arcteryx.com/us/en/c/mens'
PRODUCT = 'https://outlet.arcteryx.com/us/en/shop/mens/gamma-mx-hoody-8485'


class PageResponse(BytesIO):
    def __init__(self, url, body, headers=None):
        super().__init__(body)
        self.url = url
        self.status = 200
        self.headers = Message()
        self.headers['Content-Type'] = 'text/html'
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return self.url


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize_monitor(self.path)
        self.addCleanup(self.db.close)

    def test_numbered_links_from_anchors_and_json_without_guessing_or_prices(self):
        document = {'nested': [{'url': '/us/en/shop/mens/gamma-mx-jacket-1234'},
                               {'url': '/us/en/shop/mens/gamma-mx-hoody-8485?tracking=1'}]}
        body = ('<a href="/us/en/shop/mens/gamma-mx-hoody-8485#color">Gamma MX</a>'
                '<a href="/us/en/shop/mens/gamma-mx-hoody">No number</a>'
                '<a href="/us/en/shop/womens/gamma-mx-hoody-5555">Women</a>'
                '<a href="/ca/en/shop/mens/gamma-mx-hoody-5555">Canada</a>'
                '<a href="https://evil.example/us/en/shop/mens/gamma-mx-hoody-5555">External</a>'
                '<script type="application/json">' + json.dumps(document).replace('/', '\\/') + '</script>'
                '<script>/* no execution */ window.x = "gamma-mx-hoody-9999";</script>').encode()
        found = _listing_matches(body, CATALOG, _query_tokens('gamma MX'))
        self.assertEqual({item['url'] for item in found}, {
            PRODUCT, 'https://outlet.arcteryx.com/us/en/shop/mens/gamma-mx-jacket-1234'})
        self.assertTrue(all(set(item) == {'url', 'name'} for item in found))
        jacket = _listing_matches(body, CATALOG, _query_tokens('GammaMXJacket'))
        self.assertEqual(len(jacket), 1)
        self.assertTrue(jacket[0]['url'].endswith('jacket-1234'))

    def test_search_is_bounded_and_preserves_source_and_capture_time(self):
        def page(url):
            return b'<a href="/us/en/shop/mens/gamma-mx-hoody-8485">Gamma MX</a>', url
        with patch('arcteryx_prices.search._read_catalog_page', side_effect=page) as read:
            result = search_products(self.db, 'gamma mx', now=BASE)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(len(result['matches']), 2)
        self.assertTrue(result['complete'])
        self.assertFalse(result['exhaustive'])
        for item in result['matches']:
            self.assertEqual(datetime.fromisoformat(item['captured_at'].replace('Z', '+00:00')),
                             datetime.fromisoformat(BASE.replace('Z', '+00:00')))
            self.assertIn('/us/en/c/mens', item['source_url'])
            self.assertNotIn('price_minor', item)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_watches').fetchone()[0], 0)

    def test_inline_hydration_decodes_literal_links_without_execution_or_fragment_joining(self):
        payload = {'items': [
            {'href': '/us/en/shop/mens/gamma-mx-hoody-8485'},
            {'href': 'https://evil.example/us/en/shop/mens/gamma-mx-hoody-9001'},
            {'href': '//evil.example/us/en/shop/mens/gamma-mx-hoody-9002'},
            {'href': '/ca/en/shop/mens/gamma-mx-hoody-9003'},
            {'href': 'https://outlet.arcteryx.com.evil.example/us/en/shop/mens/gamma-mx-hoody-9004'},
        ]}
        hydration = '1:' + json.dumps(payload).replace('/', '\\u002f')
        body = ('<script>self.__next_f.push([1,' + json.dumps(hydration) + ']);</script>'
                "<script>const direct = '/us/en/shop/mens/gamma-mx-jacket-1234';"
                "const split = '/us/en/shop/mens/gamma-mx-hoody-' + '9999';"
                "fetch('https://evil.example/never-execute');</script>").encode()
        with patch('arcteryx_prices.collector.build_opener') as opener:
            matches = _listing_matches(body, CATALOG, _query_tokens('gamma mx'))
            opener.assert_not_called()
        self.assertEqual({item['url'] for item in matches}, {
            PRODUCT, 'https://outlet.arcteryx.com/us/en/shop/mens/gamma-mx-jacket-1234'})

    def test_blocked_source_stops_its_other_listing_and_survives_restart(self):
        def page(url):
            if 'outlet.arcteryx.com' in url:
                raise CollectorError('site_blocked', 'Synthetic challenge', http_status=403)
            return b'<a href="/us/en/shop/mens/sidewinder-jacket-1234">Sidewinder</a>', url
        with patch('arcteryx_prices.search._read_catalog_page', side_effect=page) as read:
            result = search_products(self.db, 'sidewinder', now=BASE)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(len(result['matches']), 1)
        self.assertFalse(result['complete'])
        blocked = [source for source in result['sources'] if 'outlet' in source['url']]
        self.assertEqual([source['attempted'] for source in blocked], [True, False])
        self.assertTrue(all(source['status'] == 'site_blocked' for source in blocked))
        with closing(initialize_monitor(self.path)) as reopened, \
                patch('arcteryx_prices.search._read_catalog_page', side_effect=page) as read:
            repeated = search_products(reopened, 'sidewinder', now='2026-10-27T12:05:00Z')
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(not source['attempted'] for source in repeated['sources'] if 'outlet' in source['url']))

    def test_catalog_redirects_and_product_reader_keep_separate_narrow_policies(self):
        handler = _CatalogRedirectHandler()
        request = Request(CATALOG)
        for bad in ('https://evil.example/us/en/c/mens', 'https://arcteryx.com/us/en/c/mens',
                    'https://outlet.arcteryx.com/ca/en/c/mens', PRODUCT,
                    'http://outlet.arcteryx.com/us/en/c/mens',
                    'https://outlet.arcteryx.com/us/en/c/mens?next=https://evil.example'):
            with self.subTest(url=bad), self.assertRaises(CollectorError):
                handler.redirect_request(request, None, 302, 'Found', {}, bad)
        safe = handler.redirect_request(request, None, 302, 'Found', {}, '/us/en/c/mens/shell-jackets/')
        self.assertTrue(safe.full_url.endswith('/shell-jackets'))
        with patch('arcteryx_prices.collector.build_opener') as opener:
            with self.assertRaises(CollectorError):
                _read_page(CATALOG, 'US')
            with self.assertRaises(CollectorError):
                validate_catalog_url('https://outlet.arcteryx.com.evil.example/us/en/c/mens')
            with self.assertRaises(CollectorError):
                validate_catalog_url('https://user@outlet.arcteryx.com/us/en/c/mens')
            opener.assert_not_called()

    def test_catalog_transport_rejects_challenges_non_html_and_oversized_responses(self):
        for body, headers, expected in (
            (b'<title>Verify you are human</title><div class="g-recaptcha"></div>', {}, 'site_blocked'),
            (b'{}', {'Content-Type': 'application/json'}, 'content_type_error'),
            (b'', {'Content-Length': '5000000'}, 'response_too_large'),
        ):
            with self.subTest(expected=expected), patch('arcteryx_prices.collector.build_opener') as opener:
                response = PageResponse(CATALOG, body)
                for key, value in headers.items():
                    response.headers.replace_header(key, value) if key in response.headers else response.headers.add_header(key, value)
                opener.return_value.open.return_value = response
                with self.assertRaises(CollectorError) as error:
                    _read_catalog_page(CATALOG)
                self.assertEqual(error.exception.status, expected)
        body = BytesIO(b'private denial')
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(CATALOG, 403, 'private', {}, body)
            with self.assertRaises(CollectorError) as error:
                _read_catalog_page(CATALOG)
        self.assertEqual(error.exception.status, 'site_blocked')
        self.assertNotIn('private', str(error.exception))
        self.assertTrue(body.closed)

    def test_empty_invalid_or_excessive_results_are_not_an_absence_claim(self):
        with patch('arcteryx_prices.search._read_catalog_page', return_value=(b'<main>Loading...</main>', CATALOG)):
            result = search_products(self.db, 'gamma mx', now=BASE)
        self.assertEqual(result['matches'], [])
        self.assertTrue(all(source['status'] == 'no_matches' for source in result['sources']))
        self.assertIn('do not prove', result['message'])
        with patch('arcteryx_prices.search._read_catalog_page') as read:
            for query in ('', '-', '../gamma', 'gamma\nmx', 'x' * 101, None):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    search_products(self.db, query)
            read.assert_not_called()
        body = b''.join(f'<a href="/us/en/shop/mens/gamma-mx-hoody-{number}">x</a>'.encode()
                        for number in range(70))
        with patch('arcteryx_prices.search._read_catalog_page', return_value=(body, CATALOG)):
            truncated = search_products(self.db, 'gamma mx', now=BASE)
        self.assertEqual(len(truncated['matches']), 64)
        self.assertTrue(truncated['truncated'])
        self.assertFalse(truncated['complete'])

    def test_product_discovery_prices_include_actual_capture_time_and_source(self):
        product = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
        fixture = Path(__file__).parent / 'fixtures' / 'collector_synthetic.html'
        before = datetime.now(timezone.utc)
        with patch('arcteryx_prices.collector._read_page', return_value=(fixture.read_bytes(), product)):
            result = discover_variants(product)
        after = datetime.now(timezone.utc)
        captured = datetime.fromisoformat(result['captured_at'].replace('Z', '+00:00'))
        self.assertLessEqual(before, captured)
        self.assertLessEqual(captured, after)
        self.assertEqual(result['source_url'], product)
        self.assertTrue(result['variants'])


if __name__ == '__main__':
    unittest.main()
