"""Finite scheduled cycles, isolated databases and no external email/network."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from arcteryx_prices.__main__ import main
from arcteryx_prices.collector import CollectorError
from arcteryx_prices.monitor import create_watch, initialize_monitor
from arcteryx_prices.notifications import GmailConfig
from arcteryx_prices.runner import cycle_lock, run_once


BASE = '2026-10-27T12:00:00Z'
URL = 'https://arcteryx.com/us/en/shop/mens/synthetic-shell'
DELIVERY = {'sent': 0, 'failed': 0, 'pending': 0, 'expired': 0, 'errors': []}


def quote(watch):
    return {'price_minor': 45000, 'availability': 'in_stock', 'observed_at': BASE,
            'captured_at': BASE, 'source_url': watch['url'], 'evidence_sha256': 'a' * 64,
            'sku': watch['sku'], 'color': watch['color'], 'size': watch['size'],
            'market': 'US', 'currency': 'USD'}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prices.sqlite3'

    def watch(self):
        connection = initialize_monitor(self.path)
        try:
            return create_watch(connection, {'url': URL, 'sku': 'TEST-M', 'color': 'Black',
                'size': 'M', 'label': 'Synthetic watch', 'rule': 'target', 'target_price_minor': 50000})
        finally:
            connection.close()

    def test_collects_confirms_then_delivers_without_running_server(self):
        self.watch()
        def delivered(connection, config, **kwargs):
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM monitor_alerts').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM monitor_checks').fetchone()[0], 2)
            return DELIVERY | {'sent': 1}
        with patch('arcteryx_prices.runner.deliver_notifications', side_effect=delivered):
            result, code = run_once(self.path, object(), now=BASE, fetcher=quote)
        self.assertEqual(code, 0)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['alerts_created'], 1)
        self.assertEqual(result['notifications']['sent'], 1)

    def test_full_dry_run_then_send_then_restart_sends_one_confirmed_event(self):
        self.watch()
        config = GmailConfig(user='sender@example.com', recipient='owner@example.com', password='abcdefghijklmnop')
        with patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.send_message.return_value = {}
            preview, preview_code = run_once(self.path, config, now=BASE, dry_run=True, fetcher=quote)
            smtp.assert_not_called()
            self.assertEqual(preview_code, 0)
            self.assertEqual(preview['notifications']['would_send'], 1)
            sent, sent_code = run_once(self.path, config, now=BASE, fetcher=lambda w: self.fail('not due'))
            restarted, code = run_once(self.path, config, now=BASE, fetcher=lambda w: self.fail('not due'))
        self.assertEqual((sent_code, code), (0, 0))
        self.assertEqual(sent['notifications']['sent'], 1)
        self.assertEqual(restarted['notifications']['sent'], 0)
        smtp.return_value.send_message.assert_called_once()
        mail = smtp.return_value.send_message.call_args.args[0]
        self.assertIn('USD 450.00', mail.get_content())
        self.assertIn('Color: Black', mail.get_content())
        self.assertIn('Size: M', mail.get_content())

    def test_extended_stock_states_through_collection_confirmation_and_email(self):
        config = GmailConfig(user='sender@example.com', recipient='owner@example.com', password='abcdefghijklmnop')
        for stock, available in (('LimitedAvailability', True), ('OnlineOnly', True),
                                 ('PreOrder', False), ('BackOrder', False), ('Discontinued', False)):
            with self.subTest(stock=stock):
                self.path = Path(self.temp.name) / f'{stock}.sqlite3'
                self.watch()
                product = {
                    '@type': 'Product', 'sku': 'TEST-M', 'color': 'Black', 'size': 'M',
                    'offers': {'@type': 'Offer', 'price': '450.00', 'priceCurrency': 'USD',
                               'availability': f'https://schema.org/{stock}',
                               'itemCondition': 'https://schema.org/NewCondition'},
                }
                body = ('<script type="application/ld+json">' + json.dumps(product) + '</script>').encode()
                with patch('arcteryx_prices.collector._read_page', return_value=(body, URL)) as page, \
                        patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
                    smtp.return_value.send_message.return_value = {}
                    result, code = run_once(self.path, config)
                self.assertEqual(code, 0)
                self.assertEqual(result['unhealthy_watches'], [])
                self.assertEqual(result['alerts_created'], int(available))
                self.assertEqual(result['notifications']['sent'], int(available))
                self.assertEqual(page.call_count, 2 if available else 1)
                self.assertEqual(smtp.return_value.send_message.call_count, int(available))

    def test_repeated_website_blocks_send_a_specific_health_email_without_fake_prices(self):
        self.watch()
        config = GmailConfig(user='sender@example.com', recipient='owner@example.com', password='abcdefghijklmnop')
        def blocked(*args, **kwargs):
            raise HTTPError(URL, 403, 'private error detail', {}, io.BytesIO(b'<title>Access Denied</title>'))
        with patch('arcteryx_prices.collector.build_opener') as opener, \
                patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp:
            opener.return_value.open.side_effect = blocked
            smtp.return_value.send_message.return_value = {}
            for attempt in range(3):
                result, code = run_once(self.path, config, now=f'2026-10-27T{12 + attempt}:00:00Z')
                self.assertEqual(code, 1)
                self.assertEqual(result['checks'][0]['status'], 'site_blocked')
                self.assertEqual(result['alerts_created'], 0)
                self.assertEqual(result['notifications']['sent'], int(attempt == 2))
            smtp.return_value.send_message.assert_called_once()
            message = smtp.return_value.send_message.call_args.args[0]
            self.assertIn('被网站拦截', str(message['Subject']))
            self.assertNotIn('private error detail', message.get_content())
        connection = initialize_monitor(self.path)
        try:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM monitor_alerts').fetchone()[0], 0)
        finally:
            connection.close()

    def test_empty_watch_list_is_explicitly_degraded(self):
        with patch('arcteryx_prices.runner.deliver_notifications', return_value=DELIVERY):
            result, code = run_once(self.path, object(), now=BASE)
        self.assertEqual(code, 1)
        self.assertEqual(result['enabled_watches'], 0)
        self.assertIn('Add your product', result['message'])

    def test_collection_failure_is_nonzero_and_still_attempts_health_delivery(self):
        self.watch()
        with patch('arcteryx_prices.runner.deliver_notifications', return_value=DELIVERY) as deliver:
            def failed(watch):
                raise CollectorError('parse_error', 'Synthetic missing variant')
            result, code = run_once(self.path, object(), now=BASE, fetcher=failed)
        self.assertEqual(code, 1)
        self.assertEqual(result['checks'][0]['status'], 'parse_error')
        self.assertEqual(result['unhealthy_watches'][0]['consecutive_failures'], 1)
        deliver.assert_called_once()

    def test_email_failure_and_waiting_retry_both_return_nonzero(self):
        self.watch()
        for delivery in (DELIVERY | {'failed': 1}, DELIVERY | {'pending': 1}):
            with self.subTest(delivery=delivery), patch('arcteryx_prices.runner.deliver_notifications', return_value=delivery):
                result, code = run_once(self.path, object(), now=BASE, fetcher=quote)
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'degraded')

    def test_not_due_still_delivers_pending_mail(self):
        self.watch()
        with patch('arcteryx_prices.runner.deliver_notifications', return_value=DELIVERY) as deliver:
            run_once(self.path, object(), now=BASE, fetcher=quote)
            result, code = run_once(self.path, object(), now=BASE, fetcher=lambda w: self.fail('not due'))
        self.assertEqual(code, 0)
        self.assertEqual(result['checked'], 0)
        self.assertEqual(deliver.call_count, 2)

    def test_force_respects_persisted_source_wait_and_dry_run_flag(self):
        self.watch()
        def limited(watch):
            raise CollectorError('http_error', 'Synthetic limited', http_status=429, retryable=True)
        with patch('arcteryx_prices.runner.deliver_notifications', return_value=DELIVERY) as deliver:
            run_once(self.path, object(), now=BASE, fetcher=limited)
            result, code = run_once(self.path, object(), now=BASE, force=True, dry_run=True,
                                    fetcher=lambda w: self.fail('must respect cooldown'))
        self.assertEqual(code, 1)
        self.assertEqual(result['checks'][0]['status'], 'rate_limited')
        self.assertTrue(deliver.call_args.kwargs['dry_run'])
        self.assertEqual(result['checked'], 0)

    def test_overlapping_cycle_skips_and_process_exit_releases_lock(self):
        code = ('from arcteryx_prices.runner import cycle_lock; import sys\n'
                'with cycle_lock(sys.argv[1]) as acquired: print(int(acquired))')
        environment = os.environ | {'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        def other():
            return subprocess.run([sys.executable, '-c', code, str(self.path)], env=environment,
                                  capture_output=True, text=True, check=True).stdout.strip()
        with cycle_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(other(), '0')
            with patch('arcteryx_prices.runner.initialize_monitor') as initialize:
                result, status = run_once(self.path, object())
            initialize.assert_not_called()
            self.assertEqual((result['status'], status), ('busy', 0))
        self.assertEqual(other(), '1')
        self.assertTrue(self.path.with_name(self.path.name + '.run-once.lock').exists())

    def test_bad_mail_configuration_fails_before_collection_and_hides_secret(self):
        args = ['arcteryx_prices', '--db', str(self.path), 'run-once', '--notify', 'gmail']
        stderr = io.StringIO()
        with patch.dict(os.environ, {'ARC_GMAIL_APP_PASSWORD': 'abcdefghijklmnop'}, clear=True), patch.object(sys, 'argv', args), \
                patch('arcteryx_prices.runner.run_once') as run, contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        self.assertIn('ARC_GMAIL_USER', stderr.getvalue())
        self.assertNotIn('abcdefghijklmnop', stderr.getvalue())
        run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_credential_free_cli_preview_saves_collection_without_smtp_or_delivery_records(self):
        self.watch()
        args = ['arcteryx_prices', '--db', str(self.path), 'run-once', '--dry-run']
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', args), \
                patch('arcteryx_prices.notifications.load_gmail_config') as load, \
                patch('arcteryx_prices.notifications.smtplib.SMTP_SSL') as smtp, \
                patch('arcteryx_prices.monitor._fetcher', return_value=quote), contextlib.redirect_stdout(stdout):
            main()
        load.assert_not_called()
        smtp.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertTrue(result['dry_run'])
        self.assertTrue(result['collection_persisted'])
        self.assertEqual(result['alerts_created'], 1)
        self.assertEqual(result['notifications']['would_send'], 1)
        self.assertFalse(result['notifications']['recipient_configured'])
        self.assertFalse(result['notifications']['delivery_history_applied'])
        connection = initialize_monitor(self.path)
        try:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM monitor_alerts').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM monitor_checks').fetchone()[0], 2)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='notification_deliveries'").fetchone())
        finally:
            connection.close()

    def test_basic_preview_ignores_ambient_mail_configuration(self):
        args = ['arcteryx_prices', '--db', str(self.path), 'run-once', '--notify', 'gmail', '--dry-run']
        with patch.dict(os.environ, {'ARC_GMAIL_USER': 'invalid-address', 'ARC_GMAIL_APP_PASSWORD': 'secret'}, clear=True), \
                patch('arcteryx_prices.notifications.load_gmail_config') as load, \
                patch('arcteryx_prices.runner.run_once', return_value=({'status': 'ok'}, 0)) as run, \
                patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
            main()
        load.assert_not_called()
        self.assertIsNone(run.call_args.args[1])
        self.assertTrue(run.call_args.kwargs['dry_run'])

    def test_real_cycle_requires_explicit_notify_before_settings_or_collection(self):
        args = ['arcteryx_prices', '--db', str(self.path), 'run-once']
        stderr = io.StringIO()
        with patch.object(sys, 'argv', args), patch('arcteryx_prices.notifications.load_gmail_config') as load, \
                patch('arcteryx_prices.runner.run_once') as run, contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        self.assertIn('--notify gmail', stderr.getvalue())
        self.assertIn('--dry-run', stderr.getvalue())
        load.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_explicit_preview_file_can_select_recipient_without_a_password(self):
        file = Path(self.temp.name) / 'preview.env'
        file.write_text('ARC_GMAIL_USER=sender@example.com\nARC_ALERT_TO=owner@example.com\n')
        file.chmod(0o600)
        args = ['arcteryx_prices', '--db', str(self.path), 'run-once', '--dry-run', '--env-file', str(file)]
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'argv', args), \
                patch('arcteryx_prices.runner.run_once', return_value=({'status': 'ok'}, 0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        config = run.call_args.args[1]
        self.assertEqual((config.user, config.recipient, config.password),
                         ('sender@example.com', 'owner@example.com', ''))

    def test_cli_preserves_summary_and_degraded_exit_code(self):
        args = ['arcteryx_prices', '--db', str(self.path), 'run-once', '--notify', 'gmail', '--dry-run', '--env-file', 'private.env']
        stdout = io.StringIO()
        with patch('arcteryx_prices.notifications.load_gmail_config', return_value=object()) as config, \
                patch('arcteryx_prices.runner.run_once', return_value=({'status': 'degraded'}, 1)) as run, \
                patch.object(sys, 'argv', args), contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {'status': 'degraded'})
        config.assert_called_once_with('private.env', require_password=False)
        self.assertTrue(run.call_args.kwargs['dry_run'])

    def test_explicit_gmail_test_uses_no_database_or_collection(self):
        args = ['arcteryx_prices', '--db', str(self.path), 'gmail-test', '--env-file', 'private.env']
        stdout = io.StringIO()
        config = object()
        with patch('arcteryx_prices.notifications.load_gmail_config', return_value=config) as load, \
                patch('arcteryx_prices.notifications.send_test_email', return_value={'status': 'accepted'}) as send, \
                patch.object(sys, 'argv', args), contextlib.redirect_stdout(stdout):
            main()
        load.assert_called_once_with('private.env')
        send.assert_called_once_with(config)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'accepted')
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
