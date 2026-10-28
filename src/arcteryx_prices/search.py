"""Bounded US official/Outlet listing search; links are not verified quotes."""

import json
import re
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler

from .collector import CollectorError, MAX_RESPONSE_BYTES, _read_html, validate_product_url
from .database import canonical_time


CATALOG_HOSTS = ('arcteryx.com', 'outlet.arcteryx.com')
CATALOG_PATHS = ('/us/en/c/mens', '/us/en/c/mens/softshell-jackets', '/us/en/c/mens/shell-jackets')
MAX_LISTING_LINKS = 4096
MAX_SCRIPT_DOCUMENTS = 32
MAX_INLINE_SCRIPTS = 64
MAX_JSON_VALUES = 20000
MAX_SEARCH_MATCHES = 64


def _query_tokens(query):
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 100
            or not re.fullmatch(r'[A-Za-z0-9 _-]+', query)):
        raise ValueError('search must be an English product name of 1 to 100 letters, digits, spaces or hyphens')
    # Also accept names such as GammaMXJacket, without changing product URLs.
    words = re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', query.strip())
    words = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', words)
    tokens = re.findall(r'[a-z0-9]+', words.lower())
    if not tokens or len(tokens) > 12:
        raise ValueError('search must contain between 1 and 12 name tokens')
    return tokens


def validate_catalog_url(url):
    """Catalog reads use only the fixed US men's listing pages, never user URLs."""
    if not isinstance(url, str) or len(url) > 4096 or re.search(r'\s|[\x00-\x1f\x7f]', url):
        raise CollectorError('invalid_url', 'Expected a supported US men\'s listing URL')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CollectorError('invalid_url', 'Listing URL cannot be parsed') from exc
    if (parsed.scheme != 'https' or parsed.hostname not in CATALOG_HOSTS
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.query or parsed.fragment
            or parsed.path.rstrip('/') not in CATALOG_PATHS):
        raise CollectorError('invalid_url', 'Only fixed HTTPS US men\'s listing pages are allowed')
    return {'url': urlunsplit(('https', parsed.hostname, parsed.path.rstrip('/'), '', '')),
            'market': 'US', 'retailer_key': f'catalog:{parsed.hostname}'}


class _CatalogRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = validate_catalog_url(urljoin(req.full_url, newurl))
            if target['retailer_key'] != validate_catalog_url(req.full_url)['retailer_key']:
                raise CollectorError('invalid_url', 'Listing redirect changed host')
        except CollectorError as exc:
            raise CollectorError('redirect_blocked', 'Listing redirect left the same host and fixed US listing paths') from exc
        return super().redirect_request(req, fp, code, msg, headers, target['url'])


def _read_catalog_page(url):
    location = validate_catalog_url(url)
    return _read_html(location['url'], 'US', validator=validate_catalog_url,
                      redirect_handler=_CatalogRedirectHandler())


class _ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.documents = []
        self.inline_scripts = []
        self.parts = None
        self.json_script = False

    def add_link(self, value):
        if isinstance(value, str) and '/shop/mens/' in value:
            self.links.append(value)
            if len(self.links) > MAX_LISTING_LINKS:
                raise CollectorError('document_limit', 'Listing contains too many product link candidates')

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == 'a':
            self.add_link(values.get('href'))
        elif tag.lower() == 'script':
            media = (values.get('type') or '').split(';', 1)[0].strip().lower()
            self.json_script = media in {'application/json', 'application/ld+json'}
            if self.json_script or media in {'', 'application/javascript', 'text/javascript', 'module'}:
                self.parts = []

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'script' and self.parts is not None:
            raw = ''.join(self.parts)
            self.parts = None
            if raw.strip():
                destination = self.documents if self.json_script else self.inline_scripts
                destination.append(raw)
                limit = MAX_SCRIPT_DOCUMENTS if self.json_script else MAX_INLINE_SCRIPTS
                if len(destination) > limit:
                    raise CollectorError('document_limit', 'Listing contains too many inline script documents')


def _quoted_literals(text):
    """Scan once; decode JSON string escapes without evaluating JavaScript."""
    index = 0
    while index < len(text):
        if text[index] not in {'"', "'"}:
            index += 1
            continue
        quote, start = text[index], index
        index += 1
        while index < len(text):
            char = text[index]
            index += 1
            if char == '\\':
                index += 1
            elif char == quote:
                raw = text[start:index]
                if quote == "'":
                    raw = '"' + raw[1:-1].replace('"', '\\"') + '"'
                try:
                    yield json.loads(raw)
                except ValueError:
                    pass
                break


def _inline_links(parser):
    # Next hydration can wrap JSON text in an outer self.__next_f.push string.
    # Two bounded decoding layers cover that shape, without joining fragments.
    pending = [(script, 0) for script in parser.inline_scripts]
    count = 0
    while pending:
        text, depth = pending.pop()
        for value in _quoted_literals(text):
            count += 1
            if count > MAX_JSON_VALUES:
                raise CollectorError('document_limit', 'Listing inline literals exceed the value limit')
            parser.add_link(value)
            if depth < 2 and ('"' in value or "'" in value):
                pending.append((value, depth + 1))


def _listing_matches(body, source_url, tokens):
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectorError('response_too_large', 'Listing response exceeds the size limit')
    parser = _ListingParser()
    try:
        parser.feed(body.decode('utf-8-sig', errors='strict'))
        parser.close()
        _inline_links(parser)
        count = 0
        for raw in parser.documents:
            try:
                values = [json.loads(raw)]
            except (ValueError, RecursionError):
                # One malformed script does not erase valid anchor evidence.
                continue
            while values:
                item = values.pop()
                count += 1
                if count > MAX_JSON_VALUES:
                    raise CollectorError('document_limit', 'Listing JSON exceeds the value limit')
                if isinstance(item, dict):
                    values.extend(item.values())
                elif isinstance(item, list):
                    values.extend(item)
                else:
                    parser.add_link(item)
    except UnicodeError as exc:
        raise CollectorError('parse_error', 'Listing is not valid UTF-8 HTML') from exc
    found = {}
    for value in parser.links:
        try:
            location = validate_product_url(urljoin(source_url, value))
        except (CollectorError, ValueError):
            continue
        parsed = urlsplit(location['url'])
        match = re.fullmatch(r'/us/en/shop/mens/([a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*)-([0-9]+)/?', parsed.path)
        if (parsed.hostname != urlsplit(source_url).hostname or not match
                or not all(token in match[1].lower().split('-') for token in tokens)):
            continue
        canonical = urlunsplit(('https', parsed.hostname, parsed.path.rstrip('/'), '', ''))
        found[canonical] = {'url': canonical, 'name': match[1].replace('-', ' ')}
    return list(found.values())


def search_products(connection, query, now=None):
    """Read a fixed, bounded listing set; respect persisted source waits.

    Matches establish discovered links only. Follow them through ordinary
    variant discovery before quoting a price or creating a watch. A no-match
    page may be incomplete or require JavaScript; it never proves absence.
    """
    from .monitor import _cooldown_status, _cooldown_until, _failure_metadata, _now, _record_host_failure
    tokens = _query_tokens(query)
    queried_at = canonical_time(_now(now))
    paths = CATALOG_PATHS
    if 'gamma' in tokens:
        paths = CATALOG_PATHS[:2]
    elif 'sidewinder' in tokens:
        paths = (CATALOG_PATHS[0], CATALOG_PATHS[2])
    sources, matches, truncated = [], {}, False
    for host in CATALOG_HOSTS:
        for path in paths:
            url = f'https://{host}{path}'
            instant = _now(now)
            source = {'url': url, 'checked_at': canonical_time(instant)}
            deadline = _cooldown_until(connection, url, instant)
            if deadline:
                sources.append(source | {'status': _cooldown_status(connection, url, instant),
                                         'attempted': False, 'retry_at': deadline})
                continue
            try:
                body, final_url = _read_catalog_page(url)
                captured_at = canonical_time(_now(now))
                discovered = _listing_matches(body, final_url, tokens)
                for item in discovered:
                    if item['url'] not in matches and len(matches) < MAX_SEARCH_MATCHES:
                        matches[item['url']] = item | {'source_url': final_url, 'captured_at': captured_at}
                    elif item['url'] not in matches:
                        truncated = True
                sources.append(source | {'status': 'ok' if discovered else 'no_matches', 'attempted': True,
                                         'source_url': final_url, 'captured_at': captured_at,
                                         'matched_links': len(discovered)})
            except CollectorError as exc:
                failed = _now(now)
                metadata = _failure_metadata(exc, now=failed)
                _record_host_failure(connection, url, metadata, failed, timedelta(hours=1))
                sources.append(source | {'status': exc.status, 'attempted': True, 'error': str(exc),
                                         'retry_at': _cooldown_until(connection, url, failed)})
    return {'query': query.strip(), 'queried_at': queried_at, 'matches': list(matches.values()),
            'sources': sources, 'complete': not truncated and all(source['status'] in {'ok', 'no_matches'} for source in sources),
            'exhaustive': False, 'truncated': truncated, 'match_limit': MAX_SEARCH_MATCHES,
            'message': 'Search covers only the listed pages. No matches do not prove a product is absent; '
                       'JavaScript-only content and unvisited pages may contain more products. '
                       'Discovered links contain no verified price or stock; run product discovery before quoting them.'}
