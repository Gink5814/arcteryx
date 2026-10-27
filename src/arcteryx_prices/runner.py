"""One finite collection/email cycle for a personal computer's task scheduler."""

from contextlib import contextmanager
import os
from pathlib import Path

from .collector import BatchQuoteCollector
from .monitor import initialize_monitor, run_due
from .notifications import deliver_notifications
from .scope import MARKET, CURRENCY
from .source_health import blocked_sources


@contextmanager
def cycle_lock(db_path):
    """An OS lock releases on process exit; leave the file to preserve its inode."""
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.run-once.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def run_once(db_path, config=None, *, force=False, dry_run=False, now=None, fetcher=None):
    """Return a JSON summary and exit status; never start a web server or sleep."""
    if config is None and not dry_run:
        raise ValueError('Gmail settings are required for delivery; use dry_run=True for a credential-free preview')
    with cycle_lock(db_path) as acquired:
        if not acquired:
            return {'status': 'busy', 'collection_persisted': False,
                    'message': 'Another run-once cycle holds this database lock.'}, 0
        connection = initialize_monitor(db_path)
        try:
            fetcher = fetcher if fetcher is not None else BatchQuoteCollector()
            results = run_due(connection, fetcher=fetcher, now=now, force=force)
            enabled = [dict(row) for row in connection.execute('''SELECT id,label,
                last_status,consecutive_failures FROM monitor_watches
                WHERE enabled=1 AND market=? AND currency=? ORDER BY id''', (MARKET, CURRENCY))]
            notifications = deliver_notifications(connection, config, dry_run=dry_run, now=now)
            blocked = blocked_sources(connection)
            affected = {watch_id: source for source in blocked for watch_id in source['watch_ids']}
            unhealthy = [watch | ({'last_status': 'site_blocked',
                                   'next_retry_at': affected[watch['id']]['not_before']}
                                  if watch['id'] in affected else {})
                         for watch in enabled if watch['last_status'] != 'ok' or watch['id'] in affected]
            degraded = not enabled or bool(unhealthy) or bool(notifications['failed']) or bool(notifications['pending'])
            result = {
                'status': 'degraded' if degraded else 'ok',
                'dry_run': dry_run,
                'collection_persisted': True,
                'enabled_watches': len(enabled),
                'checked': sum(bool(item['checks']) for item in results),
                'skipped': sum(not item['checks'] for item in results),
                'alerts_created': sum(item['alert'] is not None for item in results),
                # These count collector page fetches, not HTTP redirect hops or
                # per-SKU checks. A custom injected fetcher may not expose them.
                'page_fetches': fetcher.page_fetches if isinstance(fetcher, BatchQuoteCollector) else None,
                'cache_hits': fetcher.cache_hits if isinstance(fetcher, BatchQuoteCollector) else None,
                'confirmation_fetches': fetcher.confirmation_fetches if isinstance(fetcher, BatchQuoteCollector) else None,
                'blocked_sources': [{key: source[key] for key in (
                    'host', 'affected_watches', 'blocked_since', 'consecutive_blocks', 'not_before')}
                    for source in blocked],
                'all_sources_blocked': bool(enabled) and len(affected) == len(enabled),
                'checks': [{'watch_id': item['id'], 'status': item['check_status'],
                            'next_retry_at': item.get('next_retry_at')} for item in results],
                'unhealthy_watches': unhealthy,
                'notifications': notifications,
            }
            if not enabled:
                result['message'] = 'No enabled US/USD watches. Add your product, color, size and rule first.'
            elif blocked:
                result['message'] = (
                    f"{len(affected)} of {len(enabled)} watches depend on blocked sources. "
                    + ('All price monitoring is unavailable. ' if result['all_sources_blocked'] else '')
                    + 'Check retailer access and network/proxy settings; shared retry times are listed in blocked_sources. '
                    + ('Dry run: no email is sent. Configure Gmail and use run-once --notify gmail for delivery.'
                       if dry_run else 'Source outage notices use the notification delivery and retry results below.'))
            return result, int(degraded)
        finally:
            connection.close()
