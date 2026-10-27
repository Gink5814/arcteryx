"""Collector contract tests use synthetic data only; they prove no live adapter."""

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from arcteryx_prices.collector import (
    BatchQuoteCollector, CollectorError, MAX_CHALLENGE_BYTES, MAX_RESPONSE_BYTES, _StrictRedirectHandler, _parse_quote, _retry_after,
    collect_quote, discover_variants, parse_product_html, supported_retailers, validate_product_url,
)


FIXTURE = Path(__file__).parent / 'fixtures' / 'collector_synthetic.html'
URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
WATCH = {'url': URL, 'sku': 'SYNTHETIC-BLACK-M', 'color': 'Black',
         'size': 'M', 'market': 'US', 'currency': 'USD'}


def product(**overrides):
    """Synthetic Product, intentionally not an assertion about a live schema."""
    return {
        '@type': 'Product', 'sku': WATCH['sku'], 'color': 'Black', 'size': 'M',
        'offers': {'@type': 'Offer', 'price': '499.95', 'priceCurrency': 'USD',
                   'availability': 'https://schema.org/InStock',
                   'itemCondition': 'https://schema.org/NewCondition'},
    } | overrides


def html(node):
    return ('<!-- SYNTHETIC --> <script type="application/ld+json">' + json.dumps(node) + '</script>').encode()


class PageResponse(BytesIO):
    def __init__(self, body, *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers['Content-Type'] = 'text/html'
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return URL


class CollectorTests(unittest.TestCase):
    def assert_status(self, status, function, *args):
        with self.assertRaises(CollectorError) as failure:
            function(*args)
        self.assertEqual(failure.exception.status, status)

    def test_product_url_market_normalization_and_variant_query(self):
        self.assertEqual(validate_product_url(URL + '?color=Black&size=M#details'),
                         {'url': URL + '?color=Black&size=M', 'market': 'US', 'currency': 'USD',
                          'retailer_key': 'arcteryx-official', 'retailer_name': "Arc'teryx official"})
        self.assertEqual(validate_product_url('https://outlet.arcteryx.com/us/en/shop/mens/synthetic-shell')['currency'], 'USD')
        self.assertEqual(validate_product_url(URL.replace('arcteryx.com', 'ARCTERYX.COM:443'))['url'], URL)

    def test_url_scope_blocks_ssrf_and_non_product_paths(self):
        for bad in (
            'http://arcteryx.com/us/en/shop/mens/shell',
            'https://127.0.0.1/us/en/shop/mens/shell',
            'https://arcteryx.com.evil.example/us/en/shop/mens/shell',
            'https://user:pass@arcteryx.com/us/en/shop/mens/shell',
            'https://arcteryx.com:8443/us/en/shop/mens/shell',
            'https://arcteryx.com/us/fr/shop/mens/shell',
            'https://arcteryx.com/ca/en/shop/mens/shell',
            'https://outlet.arcteryx.com/ca/fr/shop/mens/shell',
            'https://arcteryx.com/us/en/shop/mens',
            'https://arcteryx.com/us/en/shop/mens/%2e%2e/shell',
            'https://arcteryx.com/us/en/shop/../mens/shell',
            'https://arcteryx.com/us/en/shop/mens/shell\n',
        ):
            with self.subTest(url=bad):
                self.assert_status('invalid_url', validate_product_url, bad)

    def test_canadian_discovery_and_collection_fail_without_network(self):
        for url in (URL.replace('/us/en/', '/ca/en/'),
                    'https://outlet.arcteryx.com/ca/fr/shop/mens/synthetic-shell'):
            with self.subTest(url=url), patch('arcteryx_prices.collector._read_page') as fetch:
                self.assert_status('invalid_url', discover_variants, url)
                self.assert_status('invalid_url', collect_quote,
                                   WATCH | {'url': url, 'market': 'CA', 'currency': 'CAD'})
                fetch.assert_not_called()
        self.assertTrue(all(item['markets'] == ['US'] for item in supported_retailers()))

    def test_redirect_cannot_reach_external_local_or_cross_market_targets(self):
        handler = _StrictRedirectHandler('US')
        request = Request(URL)
        for target in ('https://evil.example/x', 'http://127.0.0.1/x',
                       'https://arcteryx.com/ca/en/shop/mens/synthetic-shell',
                       'https://arcteryx.com/us/en/shop/mens/%2f%2e%2e'):
            self.assert_status('redirect_blocked', handler.redirect_request,
                               request, None, 302, 'Found', {}, target)
        redirected = handler.redirect_request(
            request, None, 302, 'Found', {},
            'https://outlet.arcteryx.com/us/en/shop/mens/synthetic-shell')
        self.assertEqual(redirected.full_url, 'https://outlet.arcteryx.com/us/en/shop/mens/synthetic-shell')

    def test_exact_variant_not_group_low_price(self):
        self.assertEqual(_parse_quote(FIXTURE.read_bytes(), WATCH),
                         {'price_minor': 49995, 'availability': 'in_stock'})
        large = WATCH | {'sku': 'SYNTHETIC-BLACK-L', 'size': 'L'}
        self.assertEqual(_parse_quote(FIXTURE.read_bytes(), large),
                         {'price_minor': 24995, 'availability': 'out_of_stock'})

    def test_wrong_or_missing_identity_rejected(self):
        for key in ('sku', 'color', 'size'):
            for value in (None, 'OTHER'):
                with self.subTest(key=key, value=value):
                    self.assert_status('identity_mismatch', _parse_quote, html(product(**{key: value})), WATCH)

    def test_currency_mismatch_and_non_new_conditions(self):
        node = product()
        node['offers']['priceCurrency'] = 'CAD'
        self.assert_status('currency_mismatch', _parse_quote, html(node), WATCH)
        for condition in (None, 'UsedCondition'):
            node = product()
            node['offers']['itemCondition'] = condition
            self.assert_status('condition_unknown', _parse_quote, html(node), WATCH)

    def test_parent_inventory_not_inherited_unknown_stock_fails_closed(self):
        for availability in (None, 'InStoreOnly', 'UnrecognizedAvailability'):
            node = product(availability='https://schema.org/InStock')
            node['offers']['availability'] = availability
            self.assert_status('availability_unknown', _parse_quote, html(node), WATCH)

    def test_limited_online_preorder_and_backorder_exact_offer_stock(self):
        for stock, expected in (
            ('LimitedAvailability', 'in_stock'), ('OnlineOnly', 'in_stock'),
            ('PreOrder', 'out_of_stock'), ('BackOrder', 'out_of_stock'),
            ('Discontinued', 'out_of_stock'),
        ):
            for prefix in ('', 'http://schema.org/', 'https://schema.org/'):
                with self.subTest(stock=stock, prefix=prefix):
                    node = product()
                    node['offers']['availability'] = prefix + stock
                    self.assertEqual(_parse_quote(html(node), WATCH),
                                     {'price_minor': 49995, 'availability': expected})

    def test_aggregate_and_member_prices_excluded(self):
        node = product()
        node['offers']['@type'] = 'AggregateOffer'
        node['offers']['lowPrice'] = '1.00'
        self.assert_status('unsupported_offer', _parse_quote, html(node), WATCH)
        for condition in ('eligibleCustomerType', 'eligibleQuantity', 'coupon', 'priceSpecification'):
            node = product()
            node['offers'][condition] = 'restricted'
            self.assert_status('unsupported_offer', _parse_quote, html(node), WATCH)

    def test_offer_item_identity_and_cross_market_url_not_assumed(self):
        node = product()
        node['offers']['sku'] = 'OTHER'
        self.assert_status('identity_mismatch', _parse_quote, html(node), WATCH)
        node = product()
        node['offers']['itemOffered'] = {'sku': WATCH['sku']}
        self.assert_status('identity_mismatch', _parse_quote, html(node), WATCH)
        node = product()
        node['offers']['url'] = 'https://arcteryx.com/ca/en/shop/mens/synthetic-shell'
        self.assert_status('invalid_url', _parse_quote, html(node), WATCH)

    def test_decimal_price_no_rounding_or_negative_sentinels(self):
        for price, expected in (('499.99', 49999), (499, 49900), ('1.01', 101)):
            node = product()
            node['offers']['price'] = price
            self.assertEqual(_parse_quote(html(node), WATCH)['price_minor'], expected)
        for price in ('499.999', '-1', '0', 'NaN', 'Infinity', '$499.99', True, None):
            node = product()
            node['offers']['price'] = price
            self.assert_status('price_error', _parse_quote, html(node), WATCH)

    def test_conflicting_exact_offers_fail_closed(self):
        first, second = product(), product()
        second['offers']['price'] = '399.95'
        self.assert_status('ambiguous_offer', _parse_quote, html([first, second]), WATCH)

    def test_expired_or_not_yet_valid_offers_excluded(self):
        today = datetime.now(timezone.utc).date()
        for field, value in (
            ('priceValidUntil', str(today - timedelta(days=1))),
            ('validThrough', str(today - timedelta(days=1))),
            ('validFrom', str(today + timedelta(days=1))),
            ('priceValidUntil', 'malformed'),
        ):
            node = product()
            node['offers'][field] = value
            self.assert_status('stale_offer', _parse_quote, html(node), WATCH)

    def test_graph_offer_reference_can_be_resolved_without_network(self):
        node = product()
        offer = node.pop('offers') | {'@id': '#synthetic-offer'}
        node['offers'] = {'@id': '#synthetic-offer'}
        self.assertEqual(_parse_quote(html({'@graph': [node, offer]}), WATCH)['price_minor'], 49995)

    def test_unrelated_review_graph_references_do_not_conflict_with_quote(self):
        node = product()
        node['review'] = [
            {'@type': 'Review', '@id': '#synthetic-review', 'reviewBody': 'Synthetic review'},
            {'@type': 'Review', '@id': '#synthetic-review'},
        ]
        self.assertEqual(_parse_quote(html(node), WATCH)['price_minor'], 49995)

    def test_invalid_or_missing_json_ld_is_parse_error(self):
        for body in (b'<html>challenge page</html>', b'<script type="application/ld+json">{bad}</script>', b'\xff'):
            self.assert_status('parse_error', _parse_quote, body, WATCH)

    def test_collect_quote_contains_evidence_and_rechecked_identity(self):
        body = FIXTURE.read_bytes()
        with patch('arcteryx_prices.collector._read_page', return_value=(body, URL)):
            quote = collect_quote(WATCH)
        self.assertEqual(quote['evidence_sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(quote['observed_at'], quote['captured_at'])
        self.assertTrue(quote['observed_at'].endswith('Z'))
        self.assertEqual(quote['price_minor'], 49995)
        for key in ('sku', 'color', 'size', 'market', 'currency'):
            self.assertEqual(quote[key], WATCH[key])

    def test_discovery_fetches_once_and_returns_only_exact_valid_variants(self):
        valid = product()
        missing_identity = product(sku='SYNTHETIC-NO-SIZE', size=None)
        conditional = product(sku='SYNTHETIC-MEMBERS')
        conditional['offers']['eligibleCustomerType'] = 'Members'
        unknown_stock = product(sku='SYNTHETIC-UNKNOWN-STOCK')
        unknown_stock['offers'].pop('availability')
        wrong_currency = product(sku='SYNTHETIC-WRONG-CURRENCY')
        wrong_currency['offers']['priceCurrency'] = 'CAD'
        body = html({'@type': 'ProductGroup', 'name': 'Synthetic discovery group',
                     'hasVariant': [valid, missing_identity, conditional, unknown_stock, wrong_currency]})
        with patch('arcteryx_prices.collector._read_page', return_value=(body, URL)) as fetch:
            result = discover_variants(URL)
            fetch.assert_called_once_with(URL, 'US')
        self.assertEqual(result, {'url': URL, 'market': 'US', 'currency': 'USD',
                                 'retailer_key': 'arcteryx-official', 'retailer_name': "Arc'teryx official",
                                 'label': 'Synthetic discovery group',
                                 'variants': [{'sku': WATCH['sku'], 'color': 'Black', 'size': 'M',
                                               'price_minor': 49995, 'availability': 'in_stock'}]})

    def test_discovery_missing_or_ambiguous_variants_are_not_selectable(self):
        first, conflicting = product(), product()
        conflicting['offers']['price'] = '1.00'
        for body in (html(product(size=None)), html([first, conflicting])):
            with patch('arcteryx_prices.collector._read_page', return_value=(body, URL)):
                self.assert_status('no_verifiable_variants', discover_variants, URL)

    def test_discovery_limits_document_and_variant_counts(self):
        for body, status in (
            (html([product(sku=f'SYNTHETIC-{n}') for n in range(257)]), 'variant_limit'),
            (html(product()) * 33, 'document_limit'),
        ):
            with patch('arcteryx_prices.collector._read_page', return_value=(body, URL)):
                self.assert_status(status, discover_variants, URL)

    def test_discovery_accepts_known_out_of_stock_and_deduplicates_identity(self):
        body = FIXTURE.read_bytes()
        with patch('arcteryx_prices.collector._read_page', return_value=(body, URL)):
            variants = discover_variants(URL)['variants']
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[1]['availability'], 'out_of_stock')
        with patch('arcteryx_prices.collector._read_page', return_value=(html([product(), product()]), URL)):
            self.assertEqual(len(discover_variants(URL)['variants']), 1)

    def test_watch_market_validation_happens_before_network(self):
        with patch('arcteryx_prices.collector.build_opener') as opener:
            self.assert_status('currency_mismatch', collect_quote, WATCH | {'market': 'CA', 'currency': 'CAD'})
            opener.assert_not_called()

    def test_http_and_network_failures_are_classified(self):
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.side_effect = URLError('synthetic connection problem')
            self.assert_status('network_error', collect_quote, WATCH)
            opener.return_value.open.side_effect = HTTPError(URL, 406, 'Not Acceptable', {}, BytesIO())
            self.assert_status('http_error', collect_quote, WATCH)

    def test_http_retry_metadata_does_not_retry_or_expose_response_details(self):
        for code, retryable in ((408, True), (429, True), (500, True), (503, True),
                                (504, True), (404, False), (403, False), (406, False)):
            with self.subTest(code=code), patch('arcteryx_prices.collector.build_opener') as opener:
                before = datetime.now(timezone.utc)
                opener.return_value.open.side_effect = HTTPError(
                    URL, code, 'SYNTHETIC PRIVATE RESPONSE',
                    {'Retry-After': '86400', 'Unrelated': 'SYNTHETIC PRIVATE HEADER'}, BytesIO())
                with self.assertRaises(CollectorError) as failure:
                    collect_quote(WATCH)
                error = failure.exception
                self.assertEqual(error.http_status, code)
                self.assertEqual(error.status, 'site_blocked' if code == 403 else 'http_error')
                self.assertEqual(error.response_url, URL)
                self.assertEqual(error.retryable, retryable)
                self.assertGreaterEqual(error.retry_after, before + timedelta(days=1))
                self.assertLessEqual(error.retry_after, datetime.now(timezone.utc) + timedelta(days=1))
                self.assertNotIn('PRIVATE', str(error))
                opener.return_value.open.assert_called_once()
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.side_effect = URLError('SYNTHETIC PRIVATE NETWORK DETAILS')
            with self.assertRaises(CollectorError) as failure:
                collect_quote(WATCH)
            self.assertTrue(failure.exception.retryable)
            self.assertIsNone(failure.exception.http_status)
            self.assertIsNone(failure.exception.retry_after)
            self.assertNotIn('PRIVATE', str(failure.exception))
        self.assertFalse(CollectorError('identity_mismatch', 'SYNTHETIC').retryable)

    def test_http_challenge_preserves_retry_metadata_and_hides_response_body(self):
        body = BytesIO(b'<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/a.js"></script>PRIVATE TOKEN')
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(URL, 503, 'PRIVATE RESPONSE',
                                                            {'Retry-After': '7200'}, body)
            before = datetime.now(timezone.utc)
            with self.assertRaises(CollectorError) as raised:
                collect_quote(WATCH)
        error = raised.exception
        self.assertEqual((error.status, error.http_status, error.retryable), ('site_blocked', 503, True))
        self.assertGreaterEqual(error.retry_after, before + timedelta(hours=2))
        self.assertNotIn('PRIVATE', str(error))
        self.assertNotIn('TOKEN', str(error))
        self.assertTrue(body.closed)

    def test_http_200_challenge_rejects_both_discovery_and_collection(self):
        for body, headers in (
            (b'<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/a.js"></script>', {}),
            (b'<title>Verify you are human</title><div class="g-recaptcha"></div>', {}),
            (b'PRIVATE CHALLENGE BODY', {'CF-Mitigated': 'challenge'}),
        ):
            for function, argument in ((collect_quote, WATCH), (discover_variants, URL)):
                with self.subTest(function=function.__name__, body=body):
                    response = PageResponse(body, headers=headers)
                    with patch('arcteryx_prices.collector.build_opener') as opener:
                        opener.return_value.open.return_value = response
                        with self.assertRaises(CollectorError) as raised:
                            function(argument)
                    self.assertEqual(raised.exception.status, 'site_blocked')
                    self.assertEqual(raised.exception.http_status, 200)
                    self.assertFalse(raised.exception.retryable)
                    self.assertNotIn('PRIVATE', str(raised.exception))
                    self.assertTrue(response.closed)

    def test_product_page_captcha_sdk_and_body_word_do_not_signal_blocking(self):
        body = (b'<title>Arc\'teryx Synthetic Shell</title>' + html(product()) +
                b'<footer>Contact us: complete the captcha</footer>'
                b'<script src="https://www.google.com/recaptcha/api.js"></script><div class="g-recaptcha"></div>')
        response = PageResponse(body)
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.return_value = response
            result = collect_quote(WATCH)
        self.assertEqual(result['price_minor'], 49995)
        self.assertEqual(result['availability'], 'in_stock')
        self.assertTrue(response.closed)

    def test_error_body_inspection_is_bounded_and_response_is_closed(self):
        class RecordingBody(BytesIO):
            requested = []

            def read(self, size=-1):
                self.requested.append(size)
                return super().read(size)

        body = RecordingBody(b'x' * (MAX_CHALLENGE_BYTES * 2))
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(URL, 403, 'PRIVATE', {}, body)
            with self.assertRaises(CollectorError) as raised:
                collect_quote(WATCH)
        self.assertEqual(raised.exception.status, 'site_blocked')
        self.assertIn('cause is not established', str(raised.exception))
        self.assertEqual(body.requested, [MAX_CHALLENGE_BYTES])
        self.assertTrue(body.closed)

    def test_unreadable_error_body_keeps_http_status_and_releases_response(self):
        class InterruptedBody(BytesIO):
            def read(self, size=-1):
                raise IncompleteRead(b'PRIVATE PARTIAL BODY')

        body = InterruptedBody()
        with patch('arcteryx_prices.collector.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(URL, 403, 'PRIVATE', {}, body)
            with self.assertRaises(CollectorError) as raised:
                collect_quote(WATCH)
        self.assertEqual((raised.exception.status, raised.exception.http_status), ('site_blocked', 403))
        self.assertNotIn('PRIVATE', str(raised.exception))
        self.assertTrue(body.closed)

    def test_error_response_close_failure_preserves_safe_blocked_status(self):
        for close_error in (OSError('PRIVATE CLEANUP ERROR'), IncompleteRead(b'PRIVATE CLEANUP BODY')):
            with self.subTest(close_error=type(close_error).__name__):
                response = HTTPError(URL, 403, 'PRIVATE RESPONSE', {}, BytesIO())
                with patch.object(response, 'close', side_effect=close_error) as close, \
                        patch('arcteryx_prices.collector.build_opener') as opener:
                    opener.return_value.open.side_effect = response
                    with self.assertRaises(CollectorError) as raised:
                        collect_quote(WATCH)
                close.assert_called_once()
                self.assertEqual((raised.exception.status, raised.exception.http_status), ('site_blocked', 403))
                self.assertNotIn('PRIVATE', str(raised.exception))

    def test_http_response_url_metadata_accepts_only_supported_product_locations(self):
        for url, expected in (
            (URL.replace('arcteryx.com', 'outlet.arcteryx.com'), URL.replace('arcteryx.com', 'outlet.arcteryx.com')),
            ('https://evil.example/private', None),
            (URL.replace('/us/en/', '/ca/en/'), None),
            (URL.replace('https://', 'https://user:password@'), None),
        ):
            with self.subTest(url=url), patch('arcteryx_prices.collector.build_opener') as opener:
                opener.return_value.open.side_effect = HTTPError(url, 429, 'SYNTHETIC', {}, BytesIO())
                with self.assertRaises(CollectorError) as failure:
                    collect_quote(WATCH)
                self.assertEqual(failure.exception.response_url, expected)

    def test_retry_after_seconds_and_http_dates_are_capped_at_24_hours(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(_retry_after('3600', now), now + timedelta(hours=1))
        self.assertEqual(_retry_after('Thu, 01 Jan 2026 12:00:00 GMT', now), now + timedelta(hours=12))
        self.assertEqual(_retry_after('86400', now), now + timedelta(hours=24))
        self.assertEqual(_retry_after('90000', now), now + timedelta(hours=24))
        self.assertEqual(_retry_after('Fri, 02 Jan 2026 12:00:00 GMT', now), now + timedelta(hours=24))
        self.assertEqual(_retry_after('Mon, 31 Dec 2029 12:00:00 GMT', now), now + timedelta(hours=24))
        self.assertEqual(_retry_after('Wed, 31 Dec 2025 12:00:00 GMT', now), now)
        self.assertEqual(_retry_after('0', now), now)
        self.assertEqual(_retry_after('9' * 100, now), now + timedelta(hours=24))
        for value in (None, '', '-1', '1.5', 'tomorrow', '9' * 129,
                      'Fri, 02 Jan 2026 12:00:00', 'Fri, 99 Jan 2026 12:00:00 GMT'):
            with self.subTest(value=value):
                self.assertIsNone(_retry_after(value, now))

    def test_batch_primary_shares_bytes_time_and_hash_but_not_variant_facts(self):
        large = WATCH | {'sku': 'SYNTHETIC-BLACK-L', 'size': 'L'}
        batch = BatchQuoteCollector()
        with patch('arcteryx_prices.collector._read_page', return_value=(FIXTURE.read_bytes(), URL)) as fetch:
            medium_quote = batch.collect(WATCH)
            large_quote = batch.collect(large)
            fetch.assert_called_once_with(URL, 'US')
        self.assertEqual((medium_quote['price_minor'], medium_quote['availability']), (49995, 'in_stock'))
        self.assertEqual((large_quote['price_minor'], large_quote['availability']), (24995, 'out_of_stock'))
        self.assertEqual(medium_quote['observed_at'], large_quote['observed_at'])
        self.assertEqual(medium_quote['evidence_sha256'], large_quote['evidence_sha256'])

    def test_confirmation_refetches_without_replacing_primary_or_surviving_next_cycle(self):
        body = FIXTURE.read_bytes()
        changed = body.replace(b'499.95', b'399.95')
        batch = BatchQuoteCollector()
        with patch('arcteryx_prices.collector._read_page',
                   side_effect=[(body, URL), (changed, URL), (changed, URL)]) as fetch:
            primary = batch.collect(WATCH)
            confirmation = batch.collect(WATCH, stage='confirmation')
            cached_primary = batch.collect(WATCH)
            next_cycle = BatchQuoteCollector().collect(WATCH)
            self.assertEqual(fetch.call_count, 3)
        self.assertEqual(primary, cached_primary)
        self.assertEqual(primary['price_minor'], 49995)
        self.assertEqual(confirmation['price_minor'], 39995)
        self.assertEqual(next_cycle['price_minor'], 39995)
        self.assertNotEqual(primary['evidence_sha256'], confirmation['evidence_sha256'])

    def test_variant_parse_failure_does_not_poison_other_variants_on_shared_page(self):
        batch = BatchQuoteCollector()
        with patch('arcteryx_prices.collector._read_page', return_value=(FIXTURE.read_bytes(), URL)) as fetch:
            self.assert_status('identity_mismatch', batch.collect, WATCH | {'sku': 'MISSING'})
            self.assertEqual(batch.collect(WATCH)['price_minor'], 49995)
            fetch.assert_called_once()

    def test_batch_failure_cache_preserves_retry_metadata_but_confirmation_fetches(self):
        boundary = datetime(2027, 1, 1, tzinfo=timezone.utc)
        error = CollectorError('http_error', 'Synthetic 429', http_status=429,
                               retryable=True, retry_after=boundary)
        batch = BatchQuoteCollector()
        with patch('arcteryx_prices.collector._read_page',
                   side_effect=[error, (FIXTURE.read_bytes(), URL)]) as fetch:
            for watch in (WATCH, WATCH | {'size': 'L', 'sku': 'SYNTHETIC-BLACK-L'}):
                with self.assertRaises(CollectorError) as failure:
                    batch.collect(watch)
                self.assertEqual(failure.exception.retry_after, boundary)
                self.assertEqual(failure.exception.http_status, 429)
                self.assertTrue(failure.exception.retryable)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(batch.collect(WATCH, stage='confirmation')['price_minor'], 49995)
            self.assertEqual(fetch.call_count, 2)

    def test_batch_lru_is_bounded_and_exact_queries_are_separate(self):
        batch = BatchQuoteCollector(max_pages=1)
        with patch('arcteryx_prices.collector._read_page',
                   side_effect=lambda url, market: (FIXTURE.read_bytes(), url)) as fetch:
            batch.collect(WATCH)
            batch.collect(WATCH | {'url': URL + '?color=Black'})
            batch.collect(WATCH)
            self.assertEqual(fetch.call_count, 3)
        for value in (0, 9, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BatchQuoteCollector(max_pages=value)

    def test_response_size_limit(self):
        with patch('arcteryx_prices.collector.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.geturl.return_value = URL
            response.status = 200
            response.headers.get_content_type.return_value = 'text/html'
            response.headers.get.side_effect = lambda key, default=None: 'identity' if key == 'Content-Encoding' else str(MAX_RESPONSE_BYTES + 1)
            self.assert_status('response_too_large', collect_quote, WATCH)
            response.read.assert_not_called()

    def test_rei_url_is_usd_and_only_supported_brand_product_paths(self):
        url = 'https://rei.com/product/C01234/arcteryx-synthetic-shell?sku=SYNTHETIC-M#details'
        self.assertEqual(validate_product_url(url), {
            'url': url.replace('rei.com', 'www.rei.com').split('#')[0],
            'market': 'US', 'currency': 'USD', 'retailer_key': 'rei', 'retailer_name': 'REI Co-op',
        })
        self.assertEqual([r['key'] for r in supported_retailers()], ['arcteryx-official', 'rei'])
        for bad in (
            'https://rei.com/product/123/another-brand-shell',
            'https://www.rei.com/used/p/arcteryx-shell/123',
            'https://www.rei.com/product/123/arcteryx-shell/%2e%2e',
            'https://rei.com.evil.example/product/123/arcteryx-shell',
            'https://www.rei.com/b/arcteryx',
            'https://www.rei.com/product/123',
        ):
            self.assert_status('invalid_url', validate_product_url, bad)

    def test_rei_discovery_requires_branded_group_and_exact_variant_evidence(self):
        url = 'https://www.rei.com/product/123/arcteryx-synthetic-shell'
        group = {'@type': 'ProductGroup', 'name': 'Synthetic REI shell',
                 'brand': {'@type': 'Brand', 'name': "Arc'teryx"}, 'hasVariant': [product()]}
        with patch('arcteryx_prices.collector._read_page', return_value=(html(group), url)):
            result = discover_variants(url)
        self.assertEqual(result['retailer_key'], 'rei')
        self.assertEqual(result['variants'], [{'sku': WATCH['sku'], 'color': 'Black', 'size': 'M',
                                             'price_minor': 49995, 'availability': 'in_stock'}])
        for change in ({'brand': {'name': 'Other brand'}}, {'brand': None}):
            with patch('arcteryx_prices.collector._read_page', return_value=(html(group | change), url)):
                self.assert_status('brand_mismatch', discover_variants, url)
        # A recommendation outside the brand's hasVariant is not the selected product.
        outside = product(sku='SYNTHETIC-RECOMMENDATION', size='XL')
        with patch('arcteryx_prices.collector._read_page', return_value=(html([group, outside]), url)):
            self.assertEqual(len(discover_variants(url)['variants']), 1)
        missing = product(size=None)
        with patch('arcteryx_prices.collector._read_page', return_value=(html(group | {'hasVariant': [missing]}), url)):
            self.assert_status('no_verifiable_variants', discover_variants, url)

    def test_retailer_identity_cannot_change_via_redirect_or_offer(self):
        rei_url = 'https://www.rei.com/product/123/arcteryx-synthetic-shell'
        handler = _StrictRedirectHandler('US')
        self.assert_status('redirect_blocked', handler.redirect_request,
                           Request(URL), None, 302, 'Found', {}, rei_url)
        self.assert_status('redirect_blocked', handler.redirect_request,
                           Request(rei_url), None, 302, 'Found', {}, URL)
        node = product()
        node['offers']['url'] = rei_url
        self.assert_status('identity_mismatch', _parse_quote, html(node), WATCH)

    def test_rei_crossed_out_list_price_is_not_the_current_sale_price(self):
        watch = WATCH | {'url': 'https://www.rei.com/product/123/arcteryx-synthetic-shell'}
        node = product()
        node['offers']['priceSpecification'] = {
            '@type': 'UnitPriceSpecification', 'priceType': 'https://schema.org/ListPrice',
            'priceCurrency': 'USD', 'price': '600.00',
        }
        group = {'@type': 'ProductGroup', 'brand': {'name': "Arc'teryx"}, 'hasVariant': [node]}
        self.assertEqual(_parse_quote(html(group), watch)['price_minor'], 49995)
        original = dict(node['offers']['priceSpecification'])
        for change in ({'priceType': 'SalePrice'}, {'priceCurrency': 'CAD'},
                       {'price': '1.00'}, {'eligibleCustomerType': 'Members'}):
            node['offers']['priceSpecification'] = original | change
            self.assert_status('unsupported_offer', _parse_quote, html(group), watch)

    def test_archive_validity_uses_verified_capture_time_not_current_date(self):
        node = product()
        node['offers']['validFrom'] = '2020-01-01'
        node['offers']['priceValidUntil'] = '2020-01-31'
        self.assertEqual(parse_product_html(html(node), WATCH, observed_at='2020-01-15T00:00:00Z'),
                         {'price_minor': 49995, 'availability': 'in_stock'})
        self.assert_status('stale_offer', _parse_quote, html(node), WATCH)
        for capture in ('2020-01-15', 'not-a-date', datetime(2020, 1, 15), 123):
            with self.assertRaises(CollectorError) as failure:
                parse_product_html(html(node), WATCH, observed_at=capture)
            self.assertEqual(failure.exception.status, 'invalid_timestamp')
        for capture in ('2019-12-31T12:00:00Z', '2020-02-01T00:00:00Z'):
            with self.assertRaises(CollectorError) as failure:
                parse_product_html(html(node), WATCH, observed_at=capture)
            self.assertEqual(failure.exception.status, 'stale_offer')


if __name__ == '__main__':
    unittest.main()
