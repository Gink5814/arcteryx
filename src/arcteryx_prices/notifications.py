"""Gmail delivery for the single-process run-once command, using only stdlib.

Call deliver_notifications while holding the run-once database file lock. A
stable Message-ID and durable ledger suppress normal reruns. SMTP cannot promise
exactly-once delivery: a crash after acceptance but before saving can duplicate a
message. SMTP acceptance also does not prove arrival in the recipient's inbox.
"""

from __future__ import annotations

import hashlib
import os
import re
import smtplib
import ssl
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from .database import canonical_time, parse_time


SCHEMA = Path(__file__).resolve().parents[2] / 'db' / 'notifications.sql'
CONFIG_KEYS = ('ARC_GMAIL_USER', 'ARC_GMAIL_APP_PASSWORD', 'ARC_ALERT_TO')
UTC = timezone.utc
PRICE_ALERT_RETRY_WINDOW = timedelta(days=7)


def _address(value, name):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value
    ):
        raise ValueError(f'{name} must contain one plain email address, without a display name')
    return value


@dataclass(frozen=True)
class GmailConfig:
    user: str
    recipient: str
    password: str = field(default='', repr=False)

    def __post_init__(self):
        _address(self.user, 'ARC_GMAIL_USER')
        _address(self.recipient, 'ARC_ALERT_TO')


def load_gmail_config(env_file=None, *, require_password=True):
    """Read literal KEY=value lines; the environment takes precedence over file.

    Matching outer quotes are allowed; shell expansion and execution never run.
    POSIX credential files must have no group/world permission bits (chmod 600).
    """
    values = {}
    if env_file is not None:
        try:
            with Path(env_file).open(encoding='utf-8-sig') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                    raise ValueError('Gmail env file must be a regular file no larger than 16 KiB')
                if os.name == 'posix' and info.st_mode & 0o077:
                    raise ValueError('Gmail env file must be private; run chmod 600 on it')
                contents = source.read(16385)
        except (OSError, UnicodeError) as exc:
            raise ValueError('Cannot read Gmail env file; check its path, permissions and UTF-8 encoding') from None
        for line in contents.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not separator or key not in CONFIG_KEYS or key in values:
                raise ValueError('Gmail env file needs unique ARC_GMAIL_USER, ARC_GMAIL_APP_PASSWORD or ARC_ALERT_TO assignments')
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError('Gmail env file contains an unmatched quote')
                value = value[1:-1]
            values[key] = value
    values.update({key: os.environ[key] for key in CONFIG_KEYS if key in os.environ})
    user = values.get('ARC_GMAIL_USER', '').strip()
    recipient = values.get('ARC_ALERT_TO', user).strip()
    password = values.get('ARC_GMAIL_APP_PASSWORD', '').replace(' ', '')
    if require_password and not re.fullmatch('[A-Za-z]{16}', password):
        raise ValueError('Set ARC_GMAIL_APP_PASSWORD to a 16-letter Google app password; enable Google 2-Step Verification first')
    return GmailConfig(user=user, recipient=recipient, password=password)


def _instant(value):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        return parse_time(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('now must be an aware datetime or ISO timestamp')
    return value.astimezone(UTC)


def _events(connection):
    for row in connection.execute('''SELECT a.*, w.color, w.size, w.sku, w.retailer_name
            FROM monitor_alerts a JOIN monitor_watches w ON w.id=a.watch_id
            WHERE w.enabled=1 AND w.market='US' AND w.currency='USD' AND a.currency='USD'
            ORDER BY a.id'''):
        event = dict(row)
        yield event | {'event_key': f"price:{event['id']}", 'kind': 'price'}
    for row in connection.execute('''SELECT * FROM monitor_watches
            WHERE enabled=1 AND market='US' AND currency='USD' AND consecutive_failures>=3
            ORDER BY id'''):
        watch = dict(row)
        episode = watch['last_success_at'] or watch['created_at']
        yield watch | {'event_key': f"health:{watch['id']}:{episode}", 'kind': 'health',
                       'watch_id': watch['id'], 'created_at': watch['last_checked_at'] or watch['created_at']}


def _message(event, config):
    label = ' '.join(event['label'].split())
    identity = (f"Product: {event['label']}\nRetailer: {event['retailer_name']}\n"
                f"Color: {event['color']}\nSize: {event['size']}\nSKU: {event['sku']}\n")
    if event['kind'] == 'price':
        price = f"USD {event['price_minor'] // 100}.{event['price_minor'] % 100:02d}"
        subject = f"Arc'teryx price alert: {label} — {price}"
        body = (identity + f"Rule: {event['rule']}\nVerified price: {price}\n"
                f"Confirmed at (UTC): {event['created_at']}\n\n{event['url']}\n\n"
                'This exact variant was in stock at the recorded confirmation time. '
                'Price and stock may have changed; confirm them before buying. '
                'Price excludes tax and shipping.\n')
    else:
        blocked = event['last_status'] == 'site_blocked'
        subject = (f"Arc'teryx 被网站拦截 / website access blocked: {label}" if blocked
                   else f"Arc'teryx monitoring needs attention: {label}")
        # Error text may contain arbitrary remote content. Send the stable status,
        # not an untrusted exception or server response that could expose secrets.
        status = re.sub(r'[^a-zA-Z0-9_ -]', '', event['last_status'])[:80]
        body = (identity + f"Consecutive failures: {event['consecutive_failures']}\n"
                f"Status: {status}\nLast attempt (UTC): {event['last_checked_at']}\n"
                f"Last success (UTC): {event['last_success_at'] or 'none'}\n"
                f"Next scheduled attempt (UTC): {event['next_due_at'] or 'not set'}\n\n"
                f"{event['url']}\n\nPrice and stock could not be verified. "
                'Check the monitor log and retailer page. This notice is sent once '
                'until collection succeeds and a new failure episode begins.\n')
        if blocked:
            body += ('\n被网站拦截：网站拒绝访问或返回了验证页面，当前无法核验价格和库存。\n'
                     'The website denied access or returned a verification page. '
                     'Open the product link in your browser to check access. '
                     'The monitor will use the next scheduled attempt shown above.\n')
    message = EmailMessage()
    message['From'] = config.user
    message['To'] = config.recipient
    message['Subject'] = subject
    message['Date'] = format_datetime(parse_time(event['created_at']))
    digest = hashlib.sha256((event['event_key'] + '\n' + config.recipient).encode()).hexdigest()
    message['Message-ID'] = f'<arcteryx-{digest}@{config.user.split("@", 1)[1]}>'
    message.set_content(body)
    return message


def _send(message, config):
    client = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30, context=ssl.create_default_context())
    try:
        client.login(config.user, config.password)
        refused = client.send_message(message, from_addr=config.user, to_addrs=[config.recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # QUIT failure after DATA acceptance must not turn a successful send into
        # a retry. close() releases the socket without requiring another reply.
        with suppress(OSError, smtplib.SMTPException):
            client.close()


def _safe_error(error):
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return 'gmail_auth_failed: check the Google app password and account settings'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'gmail_recipient_refused: check ARC_ALERT_TO'
    if isinstance(error, ssl.SSLError):
        return 'gmail_tls_failed: check system clock and trusted CA certificates'
    if isinstance(error, smtplib.SMTPException):
        return 'gmail_smtp_failed: Gmail did not confirm message acceptance'
    return 'gmail_connection_failed: check network access to smtp.gmail.com port 465'


def send_test_email(config):
    """Explicitly send one configuration test; never read or change price data."""
    if not re.fullmatch('[A-Za-z]{16}', config.password):
        raise ValueError('A Google app password is required for Gmail delivery')
    message = EmailMessage()
    message['From'] = config.user
    message['To'] = config.recipient
    message['Subject'] = "Arc'teryx monitor — Gmail configuration test"
    message['Date'] = format_datetime(datetime.now(UTC))
    message['Message-ID'] = make_msgid(domain=config.user.split('@', 1)[1])
    message.set_content('This is the Gmail configuration test you requested.\n\n'
                        'It contains no product quote or discount alert. '
                        'No monitoring records were read or changed.\n\n'
                        'Receiving this message verifies the email delivery path; '
                        'it does not confirm that scheduled monitoring is running.\n')
    try:
        _send(message, config)
    except (OSError, smtplib.SMTPException) as exc:
        raise ValueError(_safe_error(exc)) from None
    return {'status': 'accepted',
            'message': 'Gmail accepted the configuration test. Check the recipient inbox and spam folder; SMTP acceptance does not prove inbox delivery.'}


def deliver_notifications(connection, config, *, dry_run=False, now=None):
    """Deliver eligible events; caller must hold the run-once file lock.

    Price alerts remain eligible for seven days after confirmation. Pending failures
    retry after 30 minutes, doubling up to 6 hours. Health notices remain eligible
    while that failure episode is active. Dry runs preview only currently due
    messages and do not modify the delivery ledger or contact Gmail.
    """
    instant = _instant(now)
    stamp = canonical_time(instant)
    if not dry_run and not re.fullmatch('[A-Za-z]{16}', config.password):
        raise ValueError('A Google app password is required for Gmail delivery')
    has_ledger = connection.execute("SELECT 1 FROM sqlite_master WHERE name='notification_deliveries'").fetchone()
    if not dry_run and not has_ledger:
        connection.executescript(SCHEMA.read_text(encoding='utf-8'))
        has_ledger = True
    deliveries = {row['event_key']: dict(row) for row in connection.execute(
        'SELECT * FROM notification_deliveries WHERE recipient=?', (config.recipient,))} if has_ledger else {}
    summary = {'sent': 0, 'failed': 0, 'pending': 0, 'expired': 0, 'skipped': 0, 'would_send': 0, 'errors': []}
    if dry_run:
        summary['previews'] = []
    for event in _events(connection):
        previous = deliveries.get(event['event_key'])
        if previous and previous['status'] == 'sent':
            summary['skipped'] += 1
            continue
        created = parse_time(event['created_at'])
        if created > instant:
            summary['skipped'] += 1
            continue
        expired = event['kind'] == 'price' and instant - created > PRICE_ALERT_RETRY_WINDOW
        if not dry_run and not previous:
            with connection:
                connection.execute('''INSERT OR IGNORE INTO notification_deliveries
                    (event_key,recipient,kind,watch_id,created_at,status) VALUES (?,?,?,?,?,?)''',
                    (event['event_key'], config.recipient, event['kind'], event['watch_id'], event['created_at'],
                     'expired' if expired else 'pending'))
        if expired:
            summary['expired'] += 1
            if not dry_run and previous and (previous['status'] != 'expired' or previous['retry_at'] is not None):
                with connection:
                    connection.execute('''UPDATE notification_deliveries SET status='expired',retry_at=NULL
                        WHERE event_key=? AND recipient=?''', (event['event_key'], config.recipient))
            continue
        # Old versions expired prices after 24 hours. Reconsider those unsent
        # events within the longer window without changing dry-run records.
        if previous and previous['status'] != 'expired' and previous['retry_at'] and parse_time(previous['retry_at']) > instant:
            summary['pending'] += 1
            continue
        message = _message(event, config)
        if dry_run:
            summary['previews'].append({'event_key': event['event_key'], 'kind': event['kind'],
                                        'subject': str(message['Subject']), 'body': message.get_content()})
            summary['would_send'] += 1
            continue
        attempts = (previous['attempts'] if previous else 0) + 1
        try:
            _send(message, config)
        except (OSError, smtplib.SMTPException) as exc:
            error = _safe_error(exc)
            retry_at = canonical_time(instant + timedelta(minutes=min(360, 30 * 2**min(attempts - 1, 4))))
            with connection:
                connection.execute('''UPDATE notification_deliveries SET status='pending',attempts=?,retry_at=?,last_error=?
                    WHERE event_key=? AND recipient=?''',
                    (attempts, retry_at, error, event['event_key'], config.recipient))
            summary['failed'] += 1
            summary['pending'] += 1
            summary['errors'].append({'event_key': event['event_key'], 'error': error, 'retry_at': retry_at})
        else:
            with connection:
                connection.execute('''UPDATE notification_deliveries SET status='sent',attempts=?,sent_at=?,
                    retry_at=NULL,last_error=NULL WHERE event_key=? AND recipient=?''',
                    (attempts, stamp, event['event_key'], config.recipient))
            summary['sent'] += 1
    return summary
