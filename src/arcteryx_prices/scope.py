"""Active product scope; legacy database rows may retain older markets."""

MARKET = 'US'
CURRENCY = 'USD'


def in_scope(record):
    return record.get('market') == MARKET and record.get('currency') == CURRENCY


def require_scope(record):
    if not in_scope(record):
        raise ValueError('Only US/USD is supported; legacy non-US records are retained without monitoring')
