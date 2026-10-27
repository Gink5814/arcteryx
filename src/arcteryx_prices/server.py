"""Single-user loopback dashboard and background monitor (stdlib only)."""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .history import list_evidence
from .collector import supported_retailers
from .monitor import (check_watch, create_watch, initialize_monitor, list_alerts,
                      list_watches, run_due, update_watch)

LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).with_name('static')
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


@contextlib.contextmanager
def connection(path):
    database = initialize_monitor(path)
    database.execute('PRAGMA busy_timeout = 10000')
    try:
        yield database
    finally:
        database.close()


def worker_loop(database_path, stop, poll_seconds=30, fetcher=None):
    """Only due enabled watches are fetched; failures remain visible in checks."""
    while not stop.is_set():
        try:
            with connection(database_path) as database:
                run_due(database, fetcher=fetcher)
        except Exception:
            LOGGER.exception('Monitor cycle failed; next cycle will retry due watches')
        stop.wait(poll_seconds)


class MonitoringServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, database_path, host='127.0.0.1', port=8080, fetcher=None):
        # No authentication/multi-user access model is implemented yet.
        if host not in ('127.0.0.1', 'localhost'):
            raise ValueError('This single-user dashboard must bind to 127.0.0.1 or localhost')
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('port must be between 0 and 65535')
        self.database_path = Path(database_path)
        self.fetcher = fetcher
        self.csrf_token = secrets.token_urlsafe(32)
        with connection(database_path):
            pass
        super().__init__((host, port), Handler)
        self.allowed_hosts = {f'127.0.0.1:{self.server_port}', f'localhost:{self.server_port}'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'ArcMonitor/0.1'

    def log_message(self, format, *args):
        LOGGER.info('%s %s', self.address_string(), format % args)

    def send_body(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, status, value):
        self.send_body(status, json.dumps(value, ensure_ascii=False).encode())

    def safe_host(self):
        if self.headers.get('Host') not in self.server.allowed_hosts:
            self.json(403, {'error': 'Unrecognized local host'})
            return False
        return True

    def do_GET(self):
        if not self.safe_host():
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/state':
                with connection(self.server.database_path) as database:
                    state = {'watches': list_watches(database), 'alerts': list_alerts(database),
                             'csrf_token': self.server.csrf_token,
                             'scope': ['US/USD'], 'notifications': 'in_app',
                             'historical_evidence': list_evidence(database),
                             'supported_retailers': supported_retailers()}
                self.json(200, state)
            elif path == '/api/history-evidence':
                with connection(self.server.database_path) as database:
                    self.json(200, {'evidence': list_evidence(database)})
            elif path == '/api/health':
                with connection(self.server.database_path) as database:
                    database.execute('SELECT 1').fetchone()
                self.json(200, {'status': 'ok', 'service': 'arcteryx-monitor'})
            elif path in ASSETS:
                name, content_type = ASSETS[path]
                self.send_body(200, (STATIC / name).read_bytes(), content_type)
            else:
                self.json(404, {'error': 'Not found'})
        except (OSError, sqlite3.Error):
            LOGGER.exception('Dashboard read failed')
            self.json(500, {'error': 'Unable to load dashboard data'})

    def read_payload(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise ValueError('Content-Type must be application/json')
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('Transfer-Encoding is not supported')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise ValueError('Invalid Content-Length') from exc
        if not 0 < length <= 16384:
            raise ValueError('JSON request must be between 1 and 16384 bytes')
        self.connection.settimeout(10)
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Invalid JSON body') from exc
        if not isinstance(body, dict):
            raise ValueError('JSON body must be an object')
        return body

    def mutate(self):
        if not self.safe_host():
            return
        supplied = self.headers.get('X-CSRF-Token', '')
        if not secrets.compare_digest(supplied, self.server.csrf_token):
            self.json(403, {'error': 'Refresh the dashboard before making changes'})
            return
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            self.json(403, {'error': 'Cross-origin changes are not allowed'})
            return
        try:
            payload = self.read_payload()
            parts = urlsplit(self.path).path.strip('/').split('/')
            if parts == ['api', 'discover'] and self.command == 'POST':
                from .collector import CollectorError, discover_variants
                if set(payload) != {'url'}:
                    raise ValueError('Discovery expects only a product URL')
                try:
                    self.json(200, discover_variants(payload['url']))
                except CollectorError as exc:
                    self.json(422, {'error': str(exc), 'status': exc.status})
                return
            with connection(self.server.database_path) as database:
                if parts == ['api', 'watches'] and self.command == 'POST':
                    result = create_watch(database, payload)
                    self.json(201, result)
                elif len(parts) in (3, 4) and parts[:2] == ['api', 'watches']:
                    try:
                        watch_id = int(parts[2])
                    except ValueError as exc:
                        raise ValueError('Invalid watch id') from exc
                    if len(parts) == 3 and self.command == 'PATCH':
                        self.json(200, update_watch(database, watch_id, payload))
                    elif len(parts) == 4 and parts[3] == 'check' and self.command == 'POST':
                        if payload:
                            raise ValueError('Check request body must be empty')
                        self.json(200, check_watch(database, watch_id, fetcher=self.server.fetcher))
                    else:
                        self.json(404, {'error': 'Not found'})
                else:
                    self.json(404, {'error': 'Not found'})
        except (ValueError, KeyError, LookupError) as exc:
            self.json(400, {'error': str(exc)})
        except TimeoutError:
            self.json(408, {'error': 'Request timed out'})
        except sqlite3.Error:
            LOGGER.exception('Dashboard database write failed')
            self.json(503, {'error': 'Database temporarily unavailable; retry shortly'})
        except Exception:
            LOGGER.exception('Dashboard operation failed')
            self.json(500, {'error': 'Operation failed; check the service logs'})

    do_POST = mutate
    do_PATCH = mutate


def serve(database_path, host='127.0.0.1', port=8080, background=True):
    server = MonitoringServer(database_path, host, port)
    stop = threading.Event()
    worker = None
    if background:
        worker = threading.Thread(target=worker_loop, args=(database_path, stop), daemon=True)
        worker.start()
    LOGGER.warning('Local monitor listening on %s:%s; background checks %s', host,
                   server.server_port, 'enabled' if background else 'disabled')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=2)
