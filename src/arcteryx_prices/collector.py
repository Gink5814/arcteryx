"""Conservative retailer JSON-LD quote collector (standard library only).

This is a parser contract, not a claim that Arc'teryx publishes this complete
variant schema. Missing identity, public/new conditions or stock evidence fails
closed. AggregateOffer ranges and parent-product attributes are never inherited.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .scope import MARKET, CURRENCY


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JSONLD_DOCUMENTS = 32
MAX_JSONLD_NODES = 20000
MAX_DISCOVERY_VARIANTS = 256
REQUEST_TIMEOUT_SECONDS = 20
MAX_BATCH_PAGES = 8
ALLOWED_HOSTS = frozenset({'arcteryx.com', 'outlet.arcteryx.com', 'rei.com', 'www.rei.com'})
MARKET_CURRENCIES = {MARKET: CURRENCY}


def supported_retailers() -> list[dict]:
    """Sources with a live-verified parser, not a promise of every product."""
    return [
        {'key': 'arcteryx-official', 'name': "Arc'teryx official", 'markets': [MARKET],
         'hosts': ['arcteryx.com', 'outlet.arcteryx.com'],
         'example_url': 'https://arcteryx.com/us/en/shop/mens/beta-jacket-0868'},
        {'key': 'rei', 'name': 'REI Co-op', 'markets': ['US'],
         'hosts': ['rei.com', 'www.rei.com'],
         'example_url': 'https://www.rei.com/product/228617/arcteryx-beta-jacket-mens'},
    ]


class CollectorError(Exception):
    """A classified failure that must not become a price observation."""

    def __init__(self, status: str, message: str, *, http_status: int | None = None,
                 retryable: bool = False, retry_after: datetime | None = None,
                 response_url: str | None = None):
        self.status = status
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after
        self.response_url = response_url
        super().__init__(message)


def _retry_after(value: str | None, now: datetime | None = None) -> datetime | None:
    """Parse a server not-before value without sleeping or shortening its delay.

    Oversized/unparseable headers are ignored. A numeric delay beyond Python's
    calendar range saturates to its latest UTC instant, rather than overflowing
    into an immediate retry. Past dates permit a retry now; monitor policy may
    independently require a longer failure backoff.
    """
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    now = now or datetime.now(timezone.utc)
    if re.fullmatch(r'[0-9]+', value):
        try:
            return now + timedelta(seconds=int(value))
        except OverflowError:
            return datetime.max.replace(tzinfo=timezone.utc)
    try:
        boundary = parsedate_to_datetime(value)
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            return None
        return max(now, boundary.astimezone(timezone.utc))
    except (ValueError, TypeError, OverflowError):
        return None


def _http_failure(code: int, headers, response_url: str | None = None) -> CollectorError:
    try:
        response_url = validate_product_url(response_url)['url'] if response_url is not None else None
    except CollectorError:
        response_url = None
    return CollectorError(
        'http_error', f'Product request returned HTTP {code}', http_status=code,
        retryable=code in {408, 429} or 500 <= code <= 599,
        retry_after=_retry_after(headers.get('Retry-After')) if headers is not None else None,
        response_url=response_url,
    )


def validate_product_url(url: str) -> dict:
    """Restrict requests to verified United States retailer product paths."""
    if not isinstance(url, str) or len(url) > 4096 or re.search(r'\s|[\x00-\x1f\x7f]', url):
        raise CollectorError('invalid_url', 'URL must be a bounded HTTPS URL without whitespace')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CollectorError('invalid_url', 'URL cannot be parsed') from exc
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443)):
        raise CollectorError('invalid_url', 'Only HTTPS product URLs from supported retailers are allowed')
    # Plain product slugs prevent encoded separators, dot traversal and ambiguous
    # path normalization. At least a category and a product slug are required.
    if parsed.hostname in {'rei.com', 'www.rei.com'}:
        if not re.fullmatch(r'/product/[A-Za-z0-9]+/arcteryx-[A-Za-z0-9-]+/?', parsed.path):
            raise CollectorError('invalid_url', 'Expected an REI Arc\'teryx product path')
        market, host = 'US', 'www.rei.com'
        retailer = {'retailer_key': 'rei', 'retailer_name': 'REI Co-op'}
    else:
        match = re.fullmatch(r'/us/en/shop/([A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+/?', parsed.path)
        if not match:
            raise CollectorError('invalid_url', 'Only US English product paths are supported')
        market = MARKET
        host = parsed.hostname
        retailer = {'retailer_key': 'arcteryx-official', 'retailer_name': "Arc'teryx official"}
    normalized = urlunsplit(('https', host, parsed.path, parsed.query, ''))
    return {'url': normalized, 'market': market, 'currency': MARKET_CURRENCIES[market]} | retailer


class _StrictRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, market: str):
        self.market = market

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = validate_product_url(urljoin(req.full_url, newurl))
        except CollectorError as exc:
            raise CollectorError('redirect_blocked', 'Redirect left the allowed HTTPS product hosts/paths') from exc
        if target['market'] != self.market:
            raise CollectorError('redirect_blocked', 'Redirect changed the watched market')
        if target['retailer_key'] != validate_product_url(req.full_url)['retailer_key']:
            raise CollectorError('redirect_blocked', 'Redirect changed the watched retailer')
        return super().redirect_request(req, fp, code, msg, headers, target['url'])


def _read_page(url: str, market: str) -> tuple[bytes, str]:
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; ArcPriceMonitor/0.1)',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Encoding': 'identity',
    })
    # Default ProxyHandler and HTTPSHandler preserve inherited proxy/CA trust.
    # No authentication values, proxy settings or TLS verification are replaced.
    opener = build_opener(_StrictRedirectHandler(market))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            final = validate_product_url(response.geturl())
            if (final['market'] != market or
                    final['retailer_key'] != validate_product_url(url)['retailer_key']):
                raise CollectorError('redirect_blocked', 'Final response changed the watched market or retailer')
            if response.status != 200:
                raise _http_failure(response.status, response.headers, final['url'])
            mime = response.headers.get_content_type()
            if mime not in {'text/html', 'application/xhtml+xml'}:
                raise CollectorError('content_type_error', 'Product response is not HTML')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise CollectorError('content_encoding_error', 'Server ignored the identity encoding request')
            length = response.headers.get('Content-Length')
            if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise CollectorError('response_too_large', 'Product response exceeds the size limit')
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise CollectorError('response_too_large', 'Product response exceeds the size limit')
            return body, final['url']
    except CollectorError:
        raise
    except HTTPError as exc:
        raise _http_failure(exc.code, exc.headers, exc.url) from exc
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        # Do not include error bodies, authentication headers or proxy details.
        raise CollectorError('network_error', f'Product request failed ({type(exc).__name__})',
                             retryable=True) from exc


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.documents = []
        self._parts = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'script':
            attributes = dict(attrs)
            media = (attributes.get('type') or '').split(';', 1)[0].strip().lower()
            if media == 'application/ld+json':
                self._parts = []

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'script' and self._parts is not None:
            self.documents.append(''.join(self._parts))
            self._parts = None


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _schema_name(value):
    if not isinstance(value, str):
        return None
    for prefix in ('https://schema.org/', 'http://schema.org/'):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value if '/' not in value and ':' not in value else None


def _has_type(node, expected):
    values = node.get('@type', [])
    return expected in [_schema_name(v) for v in (values if isinstance(values, list) else [values])]


def _text(value):
    if isinstance(value, dict):
        # SizeSpecification/DefinedTerm labels are allowed only when explicit.
        value = value.get('name')
    return ' '.join(value.split()).casefold() if isinstance(value, str) else None


def _price_minor(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise CollectorError('price_error', 'Offer price must be an explicit numeric amount')
    try:
        price = Decimal(value)
        cents = price * 100
        if not price.is_finite() or cents != cents.to_integral_value() or not (0 < cents <= 2**63 - 1):
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise CollectorError('price_error', 'Offer price must be positive with at most two decimal places') from exc


def _check_offer_dates(offer, observed_at=None):
    now = observed_at or datetime.now(timezone.utc)
    for key in ('priceValidUntil', 'validFrom', 'validThrough'):
        value = offer.get(key)
        if value is None:
            continue
        try:
            if not isinstance(value, str):
                raise ValueError
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                boundary = date.fromisoformat(value)
                invalid = boundary > now.date() if key == 'validFrom' else boundary < now.date()
            else:
                boundary = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if boundary.tzinfo is None or boundary.utcoffset() is None:
                    raise ValueError
                invalid = boundary > now if key == 'validFrom' else boundary < now
            if invalid:
                raise CollectorError('stale_offer', 'Offer is expired or not yet valid')
        except ValueError as exc:
            raise CollectorError('stale_offer', 'Offer validity date cannot be verified') from exc


def _document_nodes(body: bytes) -> tuple[list, dict]:
    parser = _JsonLdParser()
    try:
        parser.feed(body.decode('utf-8-sig', errors='strict'))
        parser.close()
        if not parser.documents:
            raise ValueError('No JSON-LD documents')
        if len(parser.documents) > MAX_JSONLD_DOCUMENTS:
            raise CollectorError('document_limit', 'Page contains too many JSON-LD documents')
        documents = [json.loads(raw, parse_float=Decimal) for raw in parser.documents]
        nodes = []
        for document in documents:
            for node in _walk(document):
                nodes.append(node)
                if len(nodes) > MAX_JSONLD_NODES:
                    raise CollectorError('document_limit', 'Page contains too many JSON-LD nodes')
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CollectorError('parse_error', 'Page does not contain valid UTF-8 Product JSON-LD') from exc

    indexed = {}
    for node in nodes:
        # Index only substantive nodes that this collector resolves. Retailer
        # pages also contain review graphs with typed @id references; those
        # neither establish a quote nor constitute conflicting quote evidence.
        if (isinstance(node.get('@id'), str) and set(node) - {'@id', '@type'}
                and (_has_type(node, 'Product') or _has_type(node, 'Offer'))):
            key = node['@id']
            if key in indexed and indexed[key] != node:
                raise CollectorError('parse_error', 'JSON-LD has conflicting node identifiers')
            indexed[key] = node
    return nodes, indexed


def _quote_from_products(products: list, indexed: dict, watch: dict, observed_at=None) -> dict:
    def resolve(value):
        return indexed.get(value.get('@id'), value) if isinstance(value, dict) else value

    # ProductGroup is a container only: its hasVariant Product nodes occur in
    # the walk above. Never inherit group SKU, color, size, offers or stock.
    matches = [node for node in products if all(
        _text(node.get(key)) == _text(watch[key]) for key in ('sku', 'color', 'size')
    )]
    if not matches:
        raise CollectorError('identity_mismatch', 'No Product explicitly matches the watched SKU, color and size')

    candidates, errors = [], []
    for product in matches:
        offers = product.get('offers', [])
        offers = offers if isinstance(offers, list) else [offers]
        if not offers:
            errors.append(CollectorError('unsupported_offer', 'Exact variant has no direct offers'))
        for raw_offer in offers:
            offer = resolve(raw_offer)
            try:
                if not isinstance(offer, dict) or not _has_type(offer, 'Offer') or _has_type(offer, 'AggregateOffer'):
                    raise CollectorError('unsupported_offer', 'Only direct Offer prices are supported; ranges are excluded')
                if any(offer.get(key) is not None for key in (
                    'eligibleCustomerType', 'eligibleQuantity', 'eligibleTransactionVolume',
                    'membershipPointsEarned', 'discountCode', 'coupon',
                )):
                    raise CollectorError('unsupported_offer', 'Conditional, member and quantity prices are excluded')
                specification = offer.get('priceSpecification')
                if specification is not None:
                    # REI publishes the crossed-out MSRP separately on sale
                    # offers. Only this observed, non-conditional ListPrice
                    # shape is allowed; the charged price remains Offer.price.
                    is_rei = validate_product_url(watch['url'])['retailer_key'] == 'rei'
                    if (not is_rei or not isinstance(specification, dict)
                            or set(specification) != {'@type', 'priceType', 'priceCurrency', 'price'}
                            or not _has_type(specification, 'UnitPriceSpecification')
                            or _schema_name(specification.get('priceType')) != 'ListPrice'
                            or specification.get('priceCurrency') != watch['currency']
                            or _price_minor(specification.get('price')) < _price_minor(offer.get('price'))):
                        raise CollectorError('unsupported_offer', 'Unverified price specifications are excluded')
                if _schema_name(offer.get('itemCondition')) != 'NewCondition':
                    raise CollectorError('condition_unknown', 'Offer must explicitly identify new item condition')
                if offer.get('priceCurrency') != watch['currency']:
                    raise CollectorError('currency_mismatch', 'Offer currency does not match the watched market')
                _check_offer_dates(offer, observed_at)
                offered_item = resolve(offer.get('itemOffered'))
                if offered_item is not None:
                    if not isinstance(offered_item, dict) or any(
                        _text(offered_item.get(key)) != _text(watch[key]) for key in ('sku', 'color', 'size')
                    ):
                        raise CollectorError('identity_mismatch', 'Offer itemOffered does not explicitly match the watched variant')
                if 'sku' in offer and _text(offer['sku']) != _text(watch['sku']):
                    raise CollectorError('identity_mismatch', 'Offer SKU does not match the watched variant')
                if 'url' in offer:
                    offer_url = validate_product_url(urljoin(watch['url'], offer['url']))
                    if offer_url['market'] != watch['market']:
                        raise CollectorError('currency_mismatch', 'Offer URL belongs to a different market')
                    if offer_url['retailer_key'] != validate_product_url(watch['url'])['retailer_key']:
                        raise CollectorError('identity_mismatch', 'Offer URL belongs to a different retailer')
                stock = _schema_name(offer.get('availability'))
                availability = {'InStock': 'in_stock', 'OutOfStock': 'out_of_stock', 'SoldOut': 'out_of_stock'}.get(stock)
                if availability is None:
                    raise CollectorError('availability_unknown', 'Exact offer does not establish current in-stock/out-of-stock status')
                candidates.append({'price_minor': _price_minor(offer.get('price')), 'availability': availability})
            except (CollectorError, TypeError, ValueError) as exc:
                errors.append(exc if isinstance(exc, CollectorError) else CollectorError('parse_error', 'Malformed direct offer'))
    if not candidates:
        raise errors[0] if errors else CollectorError('unsupported_offer', 'No supported direct offer')
    if errors or any(candidate != candidates[0] for candidate in candidates[1:]):
        raise CollectorError('ambiguous_offer', 'Exact variant has conflicting or unsupported direct offers')
    return candidates[0]


def _retailer_products(nodes: list, watch: dict) -> list:
    if validate_product_url(watch['url'])['retailer_key'] == 'rei':
        # REI also sells many other brands. Only the explicitly branded group's
        # variant nodes establish identity; unrelated recommendations never do.
        groups = [node for node in nodes if _has_type(node, 'ProductGroup')]
        if len(groups) != 1 or _text(groups[0].get('brand')) not in {"arc'teryx", 'arc’teryx'}:
            raise CollectorError('brand_mismatch', "REI page must identify one Arc'teryx ProductGroup")
        return [node for node in _walk(groups[0].get('hasVariant', [])) if _has_type(node, 'Product')]
    return [node for node in nodes if _has_type(node, 'Product')]


def _parse_quote(body: bytes, watch: dict, *, observed_at=None) -> dict:
    """Parse live or dated snapshot HTML without retrieving any linked data."""
    if observed_at is not None:
        try:
            if isinstance(observed_at, str):
                observed_at = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
            if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
                raise ValueError
            observed_at = observed_at.astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise CollectorError('invalid_timestamp', 'Snapshot time must include a UTC offset') from exc
    nodes, indexed = _document_nodes(body)
    return _quote_from_products(_retailer_products(nodes, watch), indexed, watch, observed_at)


def parse_product_html(body: bytes, watch: dict, *, observed_at=None) -> dict:
    """Verify exact variant evidence at its capture time for audited backfills.

    This does not establish snapshot provenance: the backfill importer must
    independently verify the archive URL, capture date and original retailer URL.
    Live collection always uses the current time.
    """
    return _parse_quote(body, watch, observed_at=observed_at)


def discover_variants(url: str) -> dict:
    """Fetch once and list at most 256 strictly verifiable variant offers.

    Discovery is a selection aid, not an observation import. Mixed pages may
    contain incomplete or conditional offers; those variants are excluded using
    exactly the same identity, currency, condition and stock rules as collection.
    """
    validated = validate_product_url(url)
    body, final_url = _read_page(validated['url'], validated['market'])
    nodes, indexed = _document_nodes(body)
    products = _retailer_products(nodes, validated)
    if len(products) > MAX_DISCOVERY_VARIANTS:
        raise CollectorError('variant_limit', 'Page exceeds the maximum of 256 Product nodes')
    variants, seen = [], set()
    for product in products:
        identity = {key: product.get(key) for key in ('sku', 'color', 'size')}
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256
               for value in identity.values()):
            continue
        key = tuple(_text(identity[name]) for name in ('sku', 'color', 'size'))
        if key in seen:
            continue
        seen.add(key)
        watch = identity | validated | {'url': final_url}
        try:
            quote = _quote_from_products(products, indexed, watch)
        except CollectorError:
            continue
        variants.append({name: value.strip() for name, value in identity.items()} | quote)
    if not variants:
        raise CollectorError('no_verifiable_variants', 'Page has no exact public new-item variant offers with known stock')
    result = {'url': final_url, 'market': validated['market'],
              'currency': validated['currency'], 'variants': variants,
              'retailer_key': validated['retailer_key'], 'retailer_name': validated['retailer_name']}
    groups = [node for node in nodes if _has_type(node, 'ProductGroup')]
    if len(groups) == 1 and isinstance(groups[0].get('name'), str):
        result['label'] = groups[0]['name'][:200]
    return result


def _validated_watch(watch: dict) -> dict:
    if not isinstance(watch, dict):
        raise CollectorError('invalid_watch', 'Watch must be a dictionary')
    for key in ('url', 'sku', 'color', 'size', 'market', 'currency'):
        if not isinstance(watch.get(key), str) or not watch[key].strip():
            raise CollectorError('invalid_watch', f'Watch requires {key}')
    validated = validate_product_url(watch['url'])
    if (watch['market'], watch['currency']) != (validated['market'], validated['currency']):
        raise CollectorError('currency_mismatch', 'Watch market/currency does not match its product URL')
    return watch | {'url': validated['url']}


class BatchQuoteCollector:
    """Share primary HTML within one scheduler cycle, never confirmations.

    Every SKU is parsed independently with its own color/size/stock checks. The
    cache stores at most eight bounded responses (32 MiB total HTML). Callers
    create a new collector per cycle: this is not a persistent price cache and
    cannot manufacture a new observation time from an old response.
    """

    def __init__(self, max_pages: int = MAX_BATCH_PAGES):
        if type(max_pages) is not int or not 1 <= max_pages <= MAX_BATCH_PAGES:
            raise ValueError(f'max_pages must be an integer from 1 to {MAX_BATCH_PAGES}')
        self.max_pages = max_pages
        self._pages = OrderedDict()

    def __call__(self, watch: dict) -> dict:
        return self.collect(watch)

    def collect(self, watch: dict, stage: str = 'primary') -> dict:
        if stage not in {'primary', 'confirmation'}:
            raise ValueError('stage must be primary or confirmation')
        watch = _validated_watch(watch)
        key = watch['url']
        if stage == 'primary' and key in self._pages:
            page = self._pages.pop(key)
            self._pages[key] = page
        else:
            try:
                body, final_url = _read_page(key, watch['market'])
                stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
                page = (body, final_url, stamp, hashlib.sha256(body).hexdigest())
            except CollectorError as exc:
                # Retain safe metadata, not exception tracebacks or raw headers.
                page = {'status': exc.status, 'message': str(exc), 'http_status': exc.http_status,
                        'retryable': exc.retryable, 'retry_after': exc.retry_after,
                        'response_url': exc.response_url}
            if stage == 'primary':
                self._pages[key] = page
                while len(self._pages) > self.max_pages:
                    self._pages.popitem(last=False)
        if isinstance(page, dict):
            raise CollectorError(**page)
        body, final_url, stamp, digest = page
        quote = _parse_quote(body, watch, observed_at=stamp)
        return quote | {key: watch[key] for key in ('sku', 'color', 'size', 'market', 'currency')} | {
            'observed_at': stamp, 'captured_at': stamp, 'source_url': final_url,
            'evidence_sha256': digest,
        }


def collect_quote(watch: dict) -> dict:
    """Fetch one precise public new-item offer; no cache survives this call."""
    return BatchQuoteCollector().collect(watch)
