"""Page-fetch counts use synthetic responses; no retailer requests or email."""

from contextlib import closing
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from arcteryx_prices.collector import BatchQuoteCollector, CollectorError
from arcteryx_prices.monitor import import_watches, initialize_monitor
from arcteryx_prices.runner import run_once


PRESET = json.loads((Path(__file__).resolve().parents[1] /
                    'examples/watches.gamma-mx-sidewinder.us.json').read_text(encoding='utf-8'))


def synthetic_html(url, price='400.00'):
    variants = [{
        '@type': 'Product', **{key: watch[key] for key in ('sku', 'color', 'size')},
        'offers': {'@type': 'Offer', 'price': price, 'priceCurrency': 'USD',
                   'availability': 'https://schema.org/InStock',
                   'itemCondition': 'https://schema.org/NewCondition'},
    } for watch in PRESET if watch['url'] == url]
    group = {'@type': 'ProductGroup', 'brand': {'name': "Arc'teryx"}, 'hasVariant': variants}
    return ('<!-- SYNTHETIC PRICES --> <script type="application/ld+json">' +
            json.dumps(group) + '</script>').encode()


class PageResponse(BytesIO):
    def __init__(self, url, body):
        super().__init__(body)
        self.url = url
        self.status = 200
        self.headers = Message()
        self.headers['Content-Type'] = 'text/html'

    def geturl(self):
        return self.url


class FetchCountTest(unittest.TestCase):
    def test_twenty_variant_run_once_reports_five_page_fetches(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            with closing(initialize_monitor(path)) as connection:
                import_watches(connection, PRESET)
            with patch('arcteryx_prices.collector.build_opener') as opener, \
                    patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
                opener.return_value.open.side_effect = lambda request, **kwargs: PageResponse(
                    request.full_url, synthetic_html(request.full_url))
                result, code = run_once(path, dry_run=True)
                self.assertEqual(opener.return_value.open.call_count, 5)
                smtp.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual((result['checked'], result['skipped']), (20, 0))
            self.assertEqual((result['page_fetches'], result['cache_hits'], result['confirmation_fetches']),
                             (5, 15, 0))
            self.assertEqual(result['alerts_created'], 0)
            with closing(initialize_monitor(path)) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM monitor_checks').fetchone()[0], 20)

    def test_cached_errors_count_page_fetches_separately_from_variant_failures(self):
        for mode in ('403', 'challenge'):
            with self.subTest(mode=mode):
                batch = BatchQuoteCollector()
                def response(request, **kwargs):
                    if mode == '403':
                        raise HTTPError(request.full_url, 403, 'SYNTHETIC', {}, BytesIO())
                    return PageResponse(request.full_url, b'<title>Just a moment...</title>'
                                        b'<script src="/cdn-cgi/challenge-platform/a.js"></script>')
                with patch('arcteryx_prices.collector.build_opener') as opener:
                    opener.return_value.open.side_effect = response
                    for watch in PRESET:
                        with self.assertRaises(CollectorError) as failure:
                            batch.collect(watch | {'market': 'US', 'currency': 'USD'})
                        self.assertEqual(failure.exception.status, 'site_blocked')
                    self.assertEqual(opener.return_value.open.call_count, 5)
                self.assertEqual((batch.page_fetches, batch.cache_hits, batch.confirmation_fetches), (5, 15, 0))

    def test_confirmations_fetch_fresh_pages_and_do_not_replace_primary_cache(self):
        watch = PRESET[0] | {'market': 'US', 'currency': 'USD'}
        url = watch['url']
        batch = BatchQuoteCollector()
        with patch('arcteryx_prices.collector._read_page', side_effect=[
                (synthetic_html(url, price), url) for price in ('400.00', '350.00', '300.00')]) as read:
            self.assertEqual(batch.collect(watch)['price_minor'], 40000)
            self.assertEqual(batch.collect(watch, stage='confirmation')['price_minor'], 35000)
            self.assertEqual(batch.collect(watch)['price_minor'], 40000)
            self.assertEqual(batch.collect(watch, stage='confirmation')['price_minor'], 30000)
            self.assertEqual(read.call_count, 3)
        self.assertEqual((batch.page_fetches, batch.cache_hits, batch.confirmation_fetches), (3, 1, 2))


if __name__ == '__main__':
    unittest.main()
