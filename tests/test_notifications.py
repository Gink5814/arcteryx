"""Synthetic quotes and mocked SMTP only; these tests never send email."""

import hashlib
import os
import smtplib
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from arcteryx_prices.database import canonical_time
from arcteryx_prices.monitor import check_watch, create_watch, initialize_monitor
from arcteryx_prices.notifications import GmailConfig, deliver_notifications, load_gmail_config, send_test_email


BASE = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
PASSWORD = 'abcdefghijklmnop'
URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'


class GmailConfigTest(unittest.TestCase):
    def test_environment_defaults_to_self_and_password_is_not_in_repr(self):
        with patch.dict(os.environ, {'ARC_GMAIL_USER': 'owner@gmail.com',
                                    'ARC_GMAIL_APP_PASSWORD': 'abcd efgh ijkl mnop'}, clear=True):
            config = load_gmail_config()
        self.assertEqual((config.user, config.recipient), ('owner@gmail.com', 'owner@gmail.com'))
        self.assertEqual(config.password, PASSWORD)
        self.assertNotIn(PASSWORD, repr(config))

    def test_private_literal_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'gmail.env'
            file.write_text('# private\nARC_GMAIL_USER="owner@gmail.com"\n'
                            "ARC_GMAIL_APP_PASSWORD='abcd efgh ijkl mnop'\nARC_ALERT_TO=other@example.com\n")
            file.chmod(0o600)
            with patch.dict(os.environ, {'ARC_ALERT_TO': 'override@example.com'}, clear=True):
                config = load_gmail_config(file)
            self.assertEqual(config.recipient, 'override@example.com')
            self.assertEqual(config.password, PASSWORD)
            file.chmod(0o644)
            if os.name == 'posix':
                with self.assertRaisesRegex(ValueError, 'chmod 600'):
                    load_gmail_config(file)

    def test_missing_password_dry_run_and_single_recipient_validation(self):
        with patch.dict(os.environ, {'ARC_GMAIL_USER': 'owner@gmail.com'}, clear=True):
            self.assertEqual(load_gmail_config(require_password=False).password, '')
            with self.assertRaisesRegex(ValueError, 'Google app password'):
                load_gmail_config()
        for value in ('one@example.com,two@example.com', 'Name <one@example.com>',
                      'one@example.com\r\nBcc: extra@example.com', '', 'not-mail'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                GmailConfig('owner@gmail.com', value)

    def test_windows_style_utf8_bom_and_crlf_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'gmail.env'
            file.write_bytes(('ARC_GMAIL_USER=owner@gmail.com\r\n'
                              'ARC_GMAIL_APP_PASSWORD=abcdefghijklmnop\r\n').encode('utf-8-sig'))
            file.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                config = load_gmail_config(file)
            self.assertEqual(config.recipient, 'owner@gmail.com')
            self.assertEqual(config.password, PASSWORD)

    def test_malformed_env_does_not_echo_secret_or_execute_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'gmail.env'
            marker = Path(temporary) / 'must-not-exist'
            file.write_text(f'ARC_GMAIL_APP_PASSWORD=$(touch {marker})\nARC_GMAIL_USER=owner@gmail.com\n')
            file.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError) as error:
                load_gmail_config(file)
            self.assertNotIn(str(marker), str(error.exception))
            self.assertFalse(marker.exists())


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'prices.sqlite3'
        self.db = initialize_monitor(self.path)
        self.config = GmailConfig('owner@gmail.com', 'recipient@example.com', PASSWORD)
        self.smtp = Mock()
        self.smtp.send_message.return_value = {}
        self.transport = patch('arcteryx_prices.notifications.smtplib.SMTP_SSL', return_value=self.smtp)
        self.factory = self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.db.close()
        self.temp.cleanup()

    def watch(self, sku='synthetic-m'):
        return create_watch(self.db, {'url': URL, 'sku': sku, 'color': 'Black', 'size': 'M',
                                     'label': 'Synthetic shell', 'rule': 'target', 'target_price_minor': 30000})

    def alert(self, watch=None, at=BASE):
        watch = watch or self.watch()
        quote = {'price_minor': 24983, 'availability': 'in_stock',
                 'observed_at': canonical_time(at), 'captured_at': canonical_time(at),
                 'source_url': watch['url'], 'evidence_sha256': hashlib.sha256(b'synthetic').hexdigest(),
                 'sku': watch['sku'], 'color': watch['color'], 'size': watch['size'],
                 'market': 'US', 'currency': 'USD'}
        return check_watch(self.db, watch['id'], fetcher=lambda _: quote, now=at)

    def deliver(self, at=BASE, **kwargs):
        return deliver_notifications(self.db, self.config, now=at, **kwargs)

    def ledger(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM notification_deliveries')]

    def unhealthy(self, watch, at=BASE, *, success=None):
        with self.db:
            self.db.execute('''UPDATE monitor_watches SET consecutive_failures=3,last_status='parse_error',
                last_error='private server response',last_checked_at=?,last_success_at=? WHERE id=?''',
                (canonical_time(at), canonical_time(success) if success else None, watch['id']))

    def test_no_alert_never_connects_to_smtp(self):
        self.watch()
        result = self.deliver()
        self.assertEqual((result['sent'], result['failed']), (0, 0))
        self.factory.assert_not_called()

    def test_explicit_configuration_test_has_exact_recipient_and_no_price_claim(self):
        result = send_test_email(self.config)
        self.assertEqual(result['status'], 'accepted')
        self.assertIn('does not prove inbox delivery', result['message'])
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(message['From'], self.config.user)
        self.assertEqual(message['To'], self.config.recipient)
        self.assertEqual(self.smtp.send_message.call_args.kwargs['to_addrs'], [self.config.recipient])
        self.assertIn('configuration test', message['Subject'])
        self.assertIn('no product quote or discount alert', message.get_content())
        self.assertNotIn('USD', str(message))
        self.assertNotIn(PASSWORD, str(message))
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_deliveries'").fetchone())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM monitor_alerts').fetchone()[0], 0)

    def test_configuration_test_authentication_failure_is_sanitized(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b'secret: ' + PASSWORD.encode())
        with self.assertRaisesRegex(ValueError, 'gmail_auth_failed') as raised:
            send_test_email(self.config)
        self.assertNotIn(PASSWORD, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        self.smtp.send_message.assert_not_called()

    def test_verified_price_message_tls_exact_recipient_and_restart_deduplication(self):
        result = self.alert()
        self.assertIsNotNone(result['alert'])
        self.assertEqual(len(result['checks']), 2)
        self.assertEqual(self.deliver()['sent'], 1)
        args, kwargs = self.factory.call_args
        self.assertEqual(args, ('smtp.gmail.com', 465))
        self.assertEqual(kwargs['timeout'], 30)
        self.assertTrue(kwargs['context'].check_hostname)
        self.assertEqual(kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.smtp.login.assert_called_once_with('owner@gmail.com', PASSWORD)
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(self.smtp.send_message.call_args.kwargs['to_addrs'], ['recipient@example.com'])
        self.assertEqual(message['To'], 'recipient@example.com')
        for fragment in ('USD 249.83', 'Color: Black', 'Size: M', 'SKU: synthetic-m', URL,
                         'Confirmed at (UTC)', '2026-11-01', 'tax and shipping'):
            self.assertIn(fragment, message.get_content())
        self.assertNotIn(PASSWORD, str(message))
        self.assertNotIn(PASSWORD, repr(self.ledger()))
        self.db.close()
        self.db = initialize_monitor(self.path)
        self.assertEqual(self.deliver(BASE + timedelta(minutes=1))['sent'], 0)
        self.smtp.send_message.assert_called_once()

    def test_failure_is_sanitized_and_retry_wait_survives_restart(self):
        self.alert()
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b'secret: ' + PASSWORD.encode())
        first = self.deliver()
        self.assertEqual((first['failed'], first['pending']), (1, 1))
        self.assertIn('gmail_auth_failed', first['errors'][0]['error'])
        self.assertNotIn(PASSWORD, str(first) + str(self.ledger()))
        self.db.close()
        self.db = initialize_monitor(self.path)
        self.assertEqual(self.deliver(BASE + timedelta(minutes=29))['pending'], 1)
        self.assertEqual(self.factory.call_count, 1)
        self.smtp.login.side_effect = None
        self.assertEqual(self.deliver(BASE + timedelta(minutes=30))['sent'], 1)
        self.assertEqual(self.ledger()[0]['attempts'], 2)

    def test_pending_price_expires_before_late_retry_and_old_backlog_never_sends(self):
        self.alert()
        self.smtp.send_message.side_effect = TimeoutError('secret network detail')
        self.assertEqual(self.deliver()['failed'], 1)
        self.smtp.send_message.side_effect = None
        self.assertEqual(self.deliver(BASE + timedelta(hours=25))['expired'], 1)
        self.assertEqual(self.ledger()[0]['status'], 'expired')
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.deliver(BASE + timedelta(hours=26))['sent'], 0)

    def test_dry_run_previews_without_schema_or_delivery_mutations(self):
        self.alert()
        result = self.deliver(dry_run=True)
        self.assertEqual((result['would_send'], result['sent'], result['pending']), (1, 0, 0))
        self.assertIn('USD 249.83', result['previews'][0]['body'])
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='notification_deliveries'").fetchone())
        self.factory.assert_not_called()
        self.deliver()
        before = self.ledger()
        self.deliver(dry_run=True)
        self.assertEqual(self.ledger(), before)

    def test_paused_and_legacy_canadian_watches_are_excluded(self):
        first = self.watch('paused')
        second = self.watch('legacy')
        self.alert(first)
        self.alert(second)
        with self.db:
            self.db.execute('UPDATE monitor_watches SET enabled=0 WHERE id=?', (first['id'],))
            self.db.execute("UPDATE monitor_watches SET market='CA',currency='CAD' WHERE id=?", (second['id'],))
        self.assertEqual(self.deliver()['sent'], 0)
        self.factory.assert_not_called()

    def test_health_once_per_episode_then_recovery_rearms(self):
        watch = self.watch()
        self.unhealthy(watch)
        self.assertEqual(self.deliver()['sent'], 1)
        message = self.smtp.send_message.call_args.args[0]
        self.assertIn('monitoring needs attention', message['Subject'])
        self.assertIn('parse_error', message.get_content())
        self.assertNotIn('private server response', message.get_content())
        self.assertEqual(self.deliver(BASE + timedelta(days=2))['sent'], 0)
        self.unhealthy(watch, at=BASE + timedelta(days=3), success=BASE + timedelta(days=2))
        self.assertEqual(self.deliver(BASE + timedelta(days=3))['sent'], 1)

    def test_recovered_health_failure_is_not_retried(self):
        watch = self.watch()
        self.unhealthy(watch)
        self.smtp.login.side_effect = TimeoutError('private')
        self.assertEqual(self.deliver()['failed'], 1)
        with self.db:
            self.db.execute("UPDATE monitor_watches SET consecutive_failures=0,last_status='ok' WHERE id=?", (watch['id'],))
        self.assertEqual(self.deliver(BASE + timedelta(hours=1))['pending'], 0)
        self.assertEqual(self.factory.call_count, 1)

    def test_tls_connection_and_recipient_errors_never_leak_response(self):
        self.alert()
        self.factory.side_effect = ssl.SSLError('private credential response')
        first = self.deliver()
        self.assertIn('gmail_tls_failed', first['errors'][0]['error'])
        self.assertNotIn('private', str(first))
        self.factory.side_effect = None
        self.smtp.send_message.return_value = {'recipient@example.com': (550, b'private response')}
        second = self.deliver(BASE + timedelta(minutes=30))
        self.assertIn('gmail_recipient_refused', second['errors'][0]['error'])
        self.assertNotIn('private', str(second))

    def test_retry_message_id_is_stable_and_close_failure_does_not_duplicate(self):
        self.alert()
        self.smtp.send_message.side_effect = smtplib.SMTPServerDisconnected('synthetic')
        self.deliver()
        first_id = self.smtp.send_message.call_args.args[0]['Message-ID']
        self.smtp.send_message.side_effect = None
        self.smtp.close.side_effect = OSError('synthetic close failure')
        self.assertEqual(self.deliver(BASE + timedelta(minutes=30))['sent'], 1)
        self.assertEqual(self.smtp.send_message.call_args.args[0]['Message-ID'], first_id)
        self.assertEqual(self.deliver(BASE + timedelta(minutes=31))['sent'], 0)


if __name__ == '__main__':
    unittest.main()
