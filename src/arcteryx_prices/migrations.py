"""Small, transactional upgrades for existing monitor databases."""

import re
import sqlite3


_OLD_INTERVAL = re.compile(r'\binterval_minutes\s+BETWEEN\s+30\s+AND\s+1440\b', re.IGNORECASE)
_OLD_RULES = re.compile(r"\brule\s+IN\s*\(\s*'target'\s*,\s*'new_low'\s*,\s*'restock'\s*\)", re.IGNORECASE)


def _updated_constraints(definition):
    definition = _OLD_INTERVAL.sub('interval_minutes BETWEEN 15 AND 1440', definition)
    return _OLD_RULES.sub("rule IN ('target','new_low','restock','available_or_target')", definition)


def migrate_watch_interval(connection):
    """Upgrade interval/rule CHECK constraints without changing watch identities.

    SQLite cannot alter a CHECK constraint in place. Rebuild only this table;
    its children keep their original foreign-key targets. The original table,
    rows, indexes and triggers survive unchanged if any migration step fails.
    """
    def table_sql():
        return connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='monitor_watches'"
        ).fetchone()[0]

    original = table_sql()
    if _updated_constraints(original) == original:
        return
    if connection.in_transaction:
        raise ValueError('interval migration requires a connection outside a transaction')
    foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    legacy_alter = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    connection.execute('PRAGMA foreign_keys = OFF')
    # Keep views and triggers on other tables that refer to monitor_watches
    # valid during the brief drop/rename step; their SQL does not need changing.
    connection.execute('PRAGMA legacy_alter_table = ON')
    try:
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            original = table_sql()
            if _updated_constraints(original) == original:
                return  # Another initializer finished while this one waited.
            objects = connection.execute('''SELECT sql FROM sqlite_master
                WHERE tbl_name='monitor_watches' AND type IN ('index','trigger') AND sql IS NOT NULL
                ORDER BY type,name''').fetchall()
            definition = _updated_constraints(original)
            # Preserve the existing declaration, including any additive columns.
            definition = definition.replace('monitor_watches', '_monitor_watches_interval_migration', 1)
            connection.execute(definition)
            columns = ','.join('"' + row['name'].replace('"', '""') + '"'
                               for row in connection.execute('PRAGMA table_info(monitor_watches)'))
            connection.execute(f'''INSERT INTO _monitor_watches_interval_migration ({columns})
                SELECT {columns} FROM monitor_watches''')
            connection.execute('DROP TABLE monitor_watches')
            connection.execute('ALTER TABLE _monitor_watches_interval_migration RENAME TO monitor_watches')
            for row in objects:
                connection.execute(row['sql'])
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise sqlite3.IntegrityError('interval migration would leave broken foreign keys')
    finally:
        connection.execute(f'PRAGMA legacy_alter_table = {legacy_alter}')
        connection.execute(f'PRAGMA foreign_keys = {foreign_keys}')
