"""Run with PYTHONPATH=src python3 -m arcteryx_prices --help."""

import argparse
import json
import logging
import sqlite3
import threading

from .database import import_observations, initialize, load_json, metrics
from .scope import require_scope


def main():
    parser = argparse.ArgumentParser(description='US/USD Arc’teryx price history and monitoring')
    parser.add_argument('--db', default='data/prices.sqlite3', help='SQLite path, relative to current directory')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create or validate the schema; does not seed historical prices')
    importer = commands.add_parser('import', help='Atomically import a JSON observations array')
    importer.add_argument('json_file')
    history_import = commands.add_parser('history-import', help='Import cited historical reports as references, excluded from price metrics')
    history_import.add_argument('json_file')
    commands.add_parser('history-list', help='List historical references and verified metric inclusion')
    history_backfill = commands.add_parser('history-backfill', help='Fetch and verify a primary retailer archive for one exact watched variant')
    history_backfill.add_argument('--evidence-key', required=True)
    history_backfill.add_argument('--watch-id', required=True, type=int)
    query = commands.add_parser('metrics', help='Query observed historical metrics for one variant and market')
    for name in ('product-key', 'variant-key', 'market', 'currency'):
        query.add_argument(f'--{name}', required=True)
    query.add_argument('--as-of', help='ISO 8601 timestamp with offset (default: current UTC time)')
    query.add_argument('--retailer-key', help='Restrict all metrics to a single retailer')
    query.add_argument('--offer-key', help='Restrict to one offer (requires --retailer-key)')
    dashboard = commands.add_parser('serve', help='Start the local dashboard and due-watch scheduler')
    dashboard.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', 'localhost'])
    dashboard.add_argument('--port', type=int, default=8080)
    dashboard.add_argument('--no-worker', action='store_true', help='Disable automatic background checks')
    worker = commands.add_parser('worker', help='Run only the due-watch scheduler until interrupted')
    worker.add_argument('--poll-seconds', type=int, default=30)
    commands.add_parser('watch-list', help='List watches, freshness and historical metrics')
    commands.add_parser('alerts', help='List confirmed in-app alerts (no external messages sent)')
    watch_add = commands.add_parser('watch-add', help='Add a watch from a JSON object')
    watch_add.add_argument('json_file')
    watch_update = commands.add_parser('watch-update', help='Change a watch’s enabled state or rule')
    watch_update.add_argument('watch_id', type=int)
    watch_update.add_argument('json_file')
    check = commands.add_parser('check', help='Collect and verify one watch, or all due watches')
    check.add_argument('--watch-id', type=int)
    check.add_argument('--force', action='store_true', help='Check all enabled watches even when not due')
    discover = commands.add_parser('discover', help='Read verified color/size variants from a supported retailer product page')
    discover.add_argument('url')
    args = parser.parse_args()
    connection = None
    try:
        if args.command == 'serve':
            from .server import serve
            serve(args.db, args.host, args.port, background=not args.no_worker)
            return
        if args.command == 'worker':
            from .server import worker_loop
            if not 5 <= args.poll_seconds <= 3600:
                raise ValueError('poll-seconds must be between 5 and 3600')
            logging.basicConfig(level=logging.INFO)
            stop = threading.Event()
            try:
                worker_loop(args.db, stop, args.poll_seconds)
            except KeyboardInterrupt:
                stop.set()
            return
        if args.command == 'discover':
            from .collector import discover_variants
            print(json.dumps(discover_variants(args.url), ensure_ascii=False, indent=2))
            return
        from .monitor import (check_watch, create_watch, initialize_monitor,
                              list_alerts, list_watches, run_due, update_watch)
        connection = initialize_monitor(args.db)
        if args.command == 'init':
            result = {'database': args.db, 'schema_version': 1,
                      'observations': connection.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0]}
        elif args.command == 'import':
            payload = load_json(args.json_file)
            if isinstance(payload, dict) and isinstance(payload.get('observations'), list):
                for record in payload['observations']:
                    if isinstance(record, dict):
                        require_scope(record)
            result = import_observations(connection, payload)
        elif args.command == 'history-import':
            from .history import import_evidence
            result = import_evidence(connection, load_json(args.json_file))
        elif args.command == 'history-list':
            from .history import list_evidence
            result = list_evidence(connection)
        elif args.command == 'history-backfill':
            from .history import backfill_evidence
            result = backfill_evidence(connection, args.evidence_key, args.watch_id)
        elif args.command == 'metrics':
            require_scope({'market': args.market, 'currency': args.currency})
            result = metrics(connection, product_key=args.product_key, variant_key=args.variant_key,
                             market=args.market, currency=args.currency, as_of=args.as_of,
                             retailer_key=args.retailer_key, offer_key=args.offer_key)
        elif args.command == 'watch-add':
            result = create_watch(connection, load_json(args.json_file))
        elif args.command == 'watch-update':
            result = update_watch(connection, args.watch_id, load_json(args.json_file))
        elif args.command == 'watch-list':
            result = list_watches(connection)
        elif args.command == 'alerts':
            result = list_alerts(connection)
        elif args.command == 'check':
            result = (check_watch(connection, args.watch_id) if args.watch_id is not None
                      else run_due(connection, force=args.force))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f'error: {exc}\n')
    except Exception as exc:
        from .collector import CollectorError
        if isinstance(exc, CollectorError):
            parser.exit(2, f'{exc.status}: {exc}\n')
        raise
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    main()
