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
    watch_add.add_argument('--target-price', help='Optional target in USD, for example 280 or 279.95')
    watch_import = commands.add_parser('watch-import', help='Atomically add a JSON array of watches; matching watches keep their settings')
    watch_import.add_argument('json_file')
    watch_update = commands.add_parser('watch-update', help='Change a watch’s enabled state or rule')
    watch_update.add_argument('watch_id', type=int)
    watch_update.add_argument('json_file', nargs='?')
    watch_update.add_argument('--target-price', help='Target in USD, for example 280 or 279.95; use none to clear')
    watch_update.add_argument('--rule', choices=['target', 'new_low', 'restock', 'available_or_target'])
    watch_discover = commands.add_parser('watch-discover', help='Fetch a product and add exact color/size watches without writing SKU JSON')
    watch_discover.add_argument('url')
    watch_discover.add_argument('--sizes', nargs='+', required=True, help='Exact sizes, for example XL XXL')
    watch_discover.add_argument('--colors', nargs='+', help='Exact color names; omit to select all discovered colors')
    watch_discover.add_argument('--rule', choices=['target', 'new_low', 'restock', 'available_or_target'], default='available_or_target')
    watch_discover.add_argument('--target-price', help='Optional target in USD, for example 280 or 279.95')
    watch_discover.add_argument('--interval-minutes', type=int, default=15)
    check = commands.add_parser('check', help='Collect and verify one watch, or all due watches')
    check.add_argument('--watch-id', type=int)
    check.add_argument('--force', action='store_true', help='Check all enabled watches even when not due')
    once = commands.add_parser('run-once', help='Check due watches, preview or send alerts, and exit for a task scheduler')
    once.add_argument('--notify', choices=['gmail'], help='Required for real email delivery; optional with --dry-run')
    once.add_argument('--env-file', help='Private KEY=value Gmail configuration file; environment variables take precedence')
    once.add_argument('--dry-run', action='store_true', help='Collect and SAVE real prices/checks, then preview alerts without Gmail settings or sending; delivery records stay unchanged')
    once.add_argument('--force', action='store_true', help='Check all enabled watches; still respect source cooldowns')
    mail_test = commands.add_parser('gmail-test', help='Send one explicitly labeled Gmail configuration test; does not collect prices')
    mail_test.add_argument('--env-file', help='Private KEY=value Gmail configuration file')
    discover = commands.add_parser('discover', help='Read exact variants from a product URL, or search US official/Outlet listings by name')
    discover.add_argument('url', nargs='?')
    discover.add_argument('--search', metavar='NAME', help='Search numbered men’s product links, for example "gamma mx"')
    args = parser.parse_args()
    connection = None
    try:
        if args.command == 'gmail-test':
            from .notifications import load_gmail_config, send_test_email
            result = send_test_email(load_gmail_config(args.env_file))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == 'run-once':
            from .notifications import load_gmail_config
            from .runner import run_once
            if not args.dry_run and args.notify != 'gmail':
                raise ValueError('run-once requires --notify gmail to send; use --dry-run to collect and preview without Gmail settings')
            # A basic preview must not depend on ambient credentials. An explicit
            # file opts into recipient-specific delivery history during preview.
            config = (load_gmail_config(args.env_file, require_password=not args.dry_run)
                      if not args.dry_run or args.env_file else None)
            result, code = run_once(args.db, config, force=args.force, dry_run=args.dry_run)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if code:
                raise SystemExit(code)
            return
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
        from .monitor import (check_watch, create_watch, import_watches, initialize_monitor,
                              list_alerts, list_watches, run_due, update_watch,
                              discover_for_monitor)
        connection = initialize_monitor(args.db)
        if args.command == 'init':
            result = {'database': args.db, 'schema_version': 1,
                      'observations': connection.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0]}
        elif args.command == 'discover':
            if bool(args.url) == bool(args.search):
                raise ValueError('discover requires exactly one product URL or --search NAME')
            if args.search:
                from .search import search_products
                result = search_products(connection, args.search)
                if not result['complete']:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    raise SystemExit(1)
            else:
                result = discover_for_monitor(connection, args.url)
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
            result = create_watch(connection, _watch_payload(args))
        elif args.command == 'watch-import':
            result = import_watches(connection, load_json(args.json_file))
        elif args.command == 'watch-update':
            payload = _watch_payload(args)
            if not payload:
                raise ValueError('watch-update requires a JSON settings file, --target-price or --rule')
            result = update_watch(connection, args.watch_id, payload)
        elif args.command == 'watch-discover':
            from .watch_discovery import add_discovered_watches
            result = add_discovered_watches(connection, args.url, sizes=args.sizes, colors=args.colors,
                rule=args.rule, target_price=args.target_price, interval_minutes=args.interval_minutes)
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


def _watch_payload(args):
    payload = load_json(args.json_file) if args.json_file else {}
    if not isinstance(payload, dict):
        raise ValueError('watch settings must be a JSON object')
    if args.target_price is not None:
        if 'target_price' in payload or 'target_price_minor' in payload:
            raise ValueError('set the target in the JSON file or --target-price, not both')
        payload['target_price'] = None if args.target_price.lower() == 'none' else args.target_price
    if getattr(args, 'rule', None) is not None:
        if 'rule' in payload:
            raise ValueError('set the rule in the JSON file or --rule, not both')
        payload['rule'] = args.rule
    return payload


if __name__ == '__main__':
    main()
