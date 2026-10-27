"""Collector contract tests use synthetic data only; they prove no live adapter."""

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from arcteryx_prices.collector import (
    CollectorError, MAX_RESPONSE_BYTES, _StrictRedirectHandler, _parse_quote,
    collect_quote, discover_variants, validate_product_url,
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


class CollectorTests(unittest.TestCase):
    def assert_status(self, status, function, *args):
        with self.assertRaises(CollectorError) as failure:
            function(*args)
        self.assertEqual(failure.exception.status, status)

    def test_product_url_market_normalization_and_variant_query(self):
        self.assertEqual(validate_product_url(URL + '?color=Black&size=M#details'),
                         {'url': URL + '?color=Black&size=M', 'market': 'US', 'currency': 'USD'})
        self.assertEqual(validate_product_url('https://outlet.arcteryx.com/ca/fr/shop/mens/synthetic-shell')['currency'], 'CAD')
        self.assertEqual(validate_product_url(URL.replace('arcteryx.com', 'ARCTERYX.COM:443'))['url'], URL)

    def test_url_scope_blocks_ssrf_and_non_product_paths(self):
        for bad in (
            'http://arcteryx.com/us/en/shop/mens/shell',
            'https://127.0.0.1/us/en/shop/mens/shell',
            'https://arcteryx.com.evil.example/us/en/shop/mens/shell',
            'https://user:pass@arcteryx.com/us/en/shop/mens/shell',
            'https://arcteryx.com:8443/us/en/shop/mens/shell',
            'https://arcteryx.com/us/fr/shop/mens/shell',
            'https://arcteryx.com/us/en/shop/mens',
            'https://arcteryx.com/us/en/shop/mens/%2e%2e/shell',
            'https://arcteryx.com/us/en/shop/../mens/shell',
            'https://arcteryx.com/us/en/shop/mens/shell\n',
        ):
            with self.subTest(url=bad):
                self.assert_status('invalid_url', validate_product_url, bad)

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
        for availability in (None, 'PreOrder', 'LimitedAvailability'):
            node = product(availability='https://schema.org/InStock')
            node['offers']['availability'] = availability
            self.assert_status('availability_unknown', _parse_quote, html(node), WATCH)

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
        self.assert_status('currency_mismatch', _parse_quote, html(node), WATCH)

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

    def test_response_size_limit(self):
        with patch('arcteryx_prices.collector.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.geturl.return_value = URL
            response.status = 200
            response.headers.get_content_type.return_value = 'text/html'
            response.headers.get.side_effect = lambda key, default=None: 'identity' if key == 'Content-Encoding' else str(MAX_RESPONSE_BYTES + 1)
            self.assert_status('response_too_large', collect_quote, WATCH)
            response.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
