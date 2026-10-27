"""Persistent blocked sources, scoped to currently enabled US/USD watches."""

from urllib.parse import urlsplit


def blocked_sources(connection):
    watches_by_host = {}
    for row in connection.execute('''SELECT * FROM monitor_watches
            WHERE enabled=1 AND market='US' AND currency='USD' ORDER BY id'''):
        watch = dict(row)
        watches_by_host.setdefault(urlsplit(watch['url']).hostname, []).append(watch)
    sources = []
    for row in connection.execute('''SELECT * FROM monitor_host_cooldowns
            WHERE status='site_blocked' AND blocked_since IS NOT NULL ORDER BY host'''):
        source = dict(row)
        watches = watches_by_host.get(source['host'], [])
        if watches:
            sources.append(source | {'affected_watches': len(watches),
                                     'watch_ids': [watch['id'] for watch in watches],
                                     'representative': watches[0]})
    return sources
