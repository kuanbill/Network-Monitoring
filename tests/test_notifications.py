import copy
import json
import os
from pathlib import Path
import queue
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import notifications as mail
import server


def config(**values):
    result = dict(enabled=True, username='sender@gmail.com', password='fake-app-password',
                  sender='sender@gmail.com', recipients=['admin@example.com'])
    result.update(values)
    return result


class SettingsTests(unittest.TestCase):
    def test_gmail_defaults_and_secret_contract(self):
        settings = mail.validate_settings(config())
        self.assertEqual(settings['smtp_host'], 'smtp.gmail.com')
        self.assertEqual(settings['smtp_port'], 587)
        self.assertEqual(settings['security'], 'starttls')
        self.assertEqual(mail.validate_settings({}, settings)['password'], settings['password'])
        self.assertEqual(mail.validate_settings({'password': 'replacement'}, settings)['password'], 'replacement')
        self.assertEqual(mail.validate_settings({'enabled': False, 'password': ''}, settings)['password'], '')
        with self.assertRaises(ValueError):
            mail.validate_settings({'password': ''}, settings)

    def test_validation_types_and_header_injection(self):
        for field, value in [('enabled', 1), ('smtp_port', True), ('smtp_port', 0),
                             ('smtp_host', 'smtp://host'), ('security', 'none'),
                             ('password', 'secret\r\nBcc: intruder@example.com'),
                             ('sender', 'Alice <a@example.com>'),
                             ('recipients', ['a@example.com\r\nBcc: b@example.com']),
                             ('recipients', 'a@example.com'), ('failure_threshold', 0),
                             ('recovery_threshold', 11), ('username', '\udcff')]:
            with self.subTest(field=field, value=repr(value)), self.assertRaises(ValueError):
                mail.validate_settings(config(**{field: value}))
        with self.assertRaises(ValueError):
            mail.validate_settings(config(username=''))
        with self.assertRaises(ValueError):
            mail.validate_settings(config(recipients=[]))

    def test_private_atomic_save_reload_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = mail.NotificationManager(directory)
            replace = os.replace
            def inspect(source, target):
                if os.name != 'nt':
                    self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
                replace(source, target)
            with patch.object(mail.os, 'replace', side_effect=inspect):
                public = manager.update(config())
            self.assertNotIn('password', public)
            self.assertTrue(public['password_set'])
            self.assertNotIn('fake-app-password', json.dumps(public))
            reloaded = mail.NotificationManager(directory)
            self.assertEqual(manager.settings, reloaded.settings)
            previous = copy.deepcopy(manager.settings)
            with patch.object(mail.os, 'replace', side_effect=OSError('denied')):
                with self.assertRaises(OSError):
                    manager.update({'password': 'different'})
            self.assertEqual(manager.settings, previous)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            self.assertEqual(mail.NotificationManager(directory).settings, previous)


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = mail.NotificationManager(self.directory.name)
        self.manager.update(config())
        # Deterministically drain jobs ourselves; keep real transport execution mocked.
        self.manager.worker = Mock()
        self.host = {'id': 'one', 'name': 'Host <one>', 'ip': '192.168.1.1', 'agent_token': 'never-in-mail'}
        self.sender = patch.object(mail, 'send_email', return_value=True)
        self.send = self.sender.start()

    def tearDown(self):
        self.manager.close()
        self.sender.stop()
        self.directory.cleanup()

    def observe(self, *statuses):
        for status in statuses:
            self.manager.observe(self.host, {'status': status, 'timestamp': time.time(), 'checks': []})

    def drain(self):
        while True:
            try:
                job = self.manager.jobs.get_nowait()
            except queue.Empty:
                break
            try:
                self.manager._process(job)
            finally:
                self.manager.jobs.task_done()

    def independent_sample(self, docker, website, host_status='up', docker_code=None):
        self.host.update(ip='192.168.6.23', docker_monitor_enabled=True, website_monitor_enabled=True)
        self.manager.observe(self.host, {
            'status': 'degraded', 'host_status': host_status, 'timestamp': time.time(),
            'docker': {'ok': docker, 'code': docker_code or ('active' if docker else 'inactive')},
            'website': {'ok': website, 'code': 'http', 'http_status': 204 if website else 503,
                        'label': 'https://example.invalid/?secret=never-in-mail'},
            'agent': ({'ok': False, 'error': 'offline'} if docker is None else {'ok': True, 'metrics': {
                'services': [{'name': 'docker.service', 'status': 'active' if docker else 'inactive'}]}}),
        })

    def test_docker_and_website_recover_independently(self):
        for docker, website in ((False, True), (False, False), (False, True), (True, True)):
            for _ in range(2):
                self.independent_sample(docker, website)
            self.drain()
        expected = ['Docker 服務異常', '網站異常', '網站恢復', 'Docker 服務恢復']
        self.assertEqual(self.send.call_count, 4)
        for call, subject in zip(self.send.call_args_list, expected):
            self.assertIn(subject, call.args[1])
        self.assertIn('urc.landagent.com.tw', self.send.call_args_list[1].args[2])
        self.assertNotIn('never-in-mail', self.send.call_args_list[1].args[2])
        for _ in range(2):
            self.independent_sample(False, True)
        self.drain()
        self.assertEqual(self.send.call_count, 5)
        self.assertIn('Docker 服務異常', self.send.call_args.args[1])
        self.manager.update({'notify_recovery': False})
        for docker, website in [(False, False)] * 2 + [(True, True)] * 2:
            self.independent_sample(docker, website)
            self.drain()
        self.assertEqual(self.send.call_count, 7)
        self.manager.update({'enabled': False})
        for _ in range(2):
            self.independent_sample(False, False)
        self.drain()
        self.assertEqual(self.send.call_count, 7)

    def test_no_data_breaks_threshold_without_false_docker_recovery(self):
        for docker in (False, None, False):
            self.independent_sample(docker, True, docker_code='unknown')
        self.drain()
        self.send.assert_not_called()
        self.independent_sample(False, True, docker_code='unknown')
        self.drain()
        self.assertIn('Docker 服務異常', self.send.call_args.args[1])
        self.assertIn('無法確認', self.send.call_args.args[2])
        self.assertNotIn('已停止', self.send.call_args.args[2])
        for docker in (True, None, True):
            self.independent_sample(docker, True)
        self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.independent_sample(True, True)
        self.drain()
        self.assertIn('Docker 服務恢復', self.send.call_args.args[1])
        for _ in range(2):
            self.independent_sample(None, False, host_status='down')
        self.drain()
        self.assertEqual(self.send.call_count, 4)
        self.assertIn('主機異常', self.send.call_args_list[2].args[1])
        self.assertIn('Agent', self.send.call_args_list[2].args[2])
        self.assertNotIn('docker.service', self.send.call_args_list[2].args[2])
        self.assertIn('網站異常', self.send.call_args_list[3].args[1])

    def test_forgetting_during_send_prevents_retries(self):
        def abort_old_attempt(*args):
            self.manager.forget('one', {'website'})
            raise OSError('old connection failed')

        self.send.side_effect = abort_old_attempt
        for _ in range(2):
            self.independent_sample(True, False)
        with patch.object(self.manager.stop, 'wait', return_value=False):
            self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('網站異常', self.send.call_args.args[1])

    def test_threshold_suppression_recovery_and_second_incident(self):
        self.observe('up', 'down')
        self.assertEqual(self.manager.jobs.qsize(), 0)
        self.observe('degraded')
        self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('主機異常', self.send.call_args.args[1])
        self.assertNotIn('never-in-mail', self.send.call_args.args[2])
        self.observe('down', 'degraded', 'degraded', 'up')
        self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.observe('up')
        self.drain()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('主機恢復', self.send.call_args.args[1])
        self.observe('up', 'down', 'down')
        self.drain()
        self.assertEqual(self.send.call_count, 3)

    def test_no_recovery_without_alert_and_confirmed_events_are_retained(self):
        self.observe('up', 'up', 'down', 'up', 'up')
        self.drain()
        self.send.assert_not_called()
        self.observe('down', 'down', 'up', 'up')
        self.drain()  # Even a completed incident retains its alert/recovery sequence.
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('主機異常', self.send.call_args_list[0].args[1])
        self.assertIn('無法連線', self.send.call_args_list[0].args[2])
        self.assertIn('主機恢復', self.send.call_args_list[1].args[1])
        self.assertIn('運作正常', self.send.call_args_list[1].args[2])

    def test_traditional_chinese_causes_and_no_private_error_echo(self):
        last = {'status': 'degraded', 'timestamp': time.time(), 'checks': [
            {'type': 'tcp', 'label': 'TCP 443', 'ok': False, 'detail': '[Errno 111] Connection refused'},
            {'type': 'http', 'label': 'https://192.168.1.1/private?secret=never-in-mail',
             'ok': False, 'detail': 'HTTP 503'},
            {'type': 'http', 'ok': False, 'detail': 'private-error-never-in-mail'}],
            'agent': {'ok': False, 'error': '代理程式 HTTP 401'}}
        self.manager.observe(self.host, last)
        self.manager.observe(self.host, last)
        self.drain()
        body = self.send.call_args.args[2]
        for text in ('TCP 443', '連線遭拒', 'HTTP 503', '應用程式日誌', '認證未通過',
                     '來源 IP 白名單', '連續 2 輪', '可能原因', '並非已確認的根本原因'):
            self.assertIn(text, body)
        for text in ('never-in-mail', '/private', 'Connection refused'):
            self.assertNotIn(text, body)

    def test_service_states_are_translated_without_claiming_unknown_is_stopped(self):
        last = {'status': 'degraded', 'timestamp': time.time(), 'checks': [], 'agent': {
            'ok': True, 'metrics': {'services': [
                {'name': 'sshd', 'status': 'failed'}, {'name': 'example', 'status': 'unknown'},
                {'name': 'paused-service', 'status': 'paused'}]}}}
        self.manager.observe(self.host, last)
        self.manager.observe(self.host, last)
        self.drain()
        body = self.send.call_args.args[2]
        self.assertIn('系統服務「sshd」：啟動或執行失敗', body)
        self.assertIn('無法確認狀態，不代表已確認停止', body)
        self.assertIn('已暫停', body)
        self.assertNotIn(' — failed', body)
        self.observe('up', 'up')
        self.drain()
        self.assertIn('恢復說明：已連續 2 輪檢查正常', self.send.call_args.args[2])

    def test_failure_explanation_known_causes(self):
        for detail, agent, expected in [
            ('timed out', False, '逾時'), ('SSL certificate verify failed', False, '憑證'),
            ('No route to host', False, '路由'), ('Connection reset', False, '中斷'),
            ('代理程式 HTTP 503', True, '資料過期'), ('代理程式指標格式錯誤', True, '格式不符'),
            ('HTTP 404', False, '檢查路徑'), ('HTTP 403', False, '網站權限')]:
            with self.subTest(detail=detail):
                self.assertIn(expected, mail.failure_explanation(detail, agent))

    def test_missing_status_is_not_a_recovery(self):
        self.observe('down', 'down')
        self.drain()
        self.observe('unknown', 'unknown')
        self.drain()
        self.assertTrue(self.manager.records['one', 'host']['active'])
        self.assertEqual(self.send.call_count, 1)

    def test_recovery_during_delivery_is_ordered_after_alert(self):
        def delivering(settings, subject, body):
            if '主機異常' in subject:
                self.observe('up', 'up')
            return True
        self.send.side_effect = delivering
        self.observe('down', 'down')
        self.drain()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('主機恢復', self.send.call_args_list[1].args[1])

    def test_new_incident_follows_previous_recovery_in_order(self):
        self.observe('down', 'down')
        self.drain()
        self.observe('up', 'up', 'down', 'down')
        self.drain()
        self.assertEqual(self.send.call_count, 3)
        self.assertIn('主機異常', self.send.call_args_list[0].args[1])
        self.assertIn('主機恢復', self.send.call_args_list[1].args[1])
        self.assertIn('運作正常', self.send.call_args_list[1].args[2])
        self.assertIn('主機異常', self.send.call_args_list[2].args[1])
        self.observe('up', 'up')
        self.drain()
        self.assertEqual(self.send.call_count, 4)
        self.assertIn('主機恢復', self.send.call_args.args[1])

    def test_disabled_recovery_and_settings_invalidation(self):
        self.manager.update({'notify_recovery': False})
        self.observe('down', 'down')
        self.drain()
        self.observe('up', 'up')
        self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.observe('down', 'down')
        self.manager.update({'enabled': False})
        self.drain()
        self.observe('down', 'down')
        self.assertEqual(self.send.call_count, 1)

    def test_retry_delay_limit_and_safe_errors(self):
        self.send.side_effect = RuntimeError('echo fake-app-password never expose this')
        self.observe('down', 'down')
        with patch.object(self.manager.stop, 'wait', return_value=False) as wait:
            self.drain()
        self.assertEqual(wait.call_count, mail.MAX_ATTEMPTS - 1)
        self.assertTrue(all(call.args == (mail.RETRY_DELAY,) for call in wait.call_args_list))
        for _ in range(5):
            self.manager.records['one', 'host']['retry_after'] = 0
            self.observe('down')
            self.drain()
        self.assertEqual(self.send.call_count, mail.MAX_ATTEMPTS)
        public = self.manager.public()
        self.assertEqual(public['delivery']['status'], 'failed')
        self.assertNotIn('fake-app-password', json.dumps(public))

    def test_partial_recipient_failure_is_not_retried(self):
        self.send.return_value = False
        self.observe('down', 'down')
        self.drain()
        self.manager.records['one', 'host']['retry_after'] = 0
        self.observe('down')
        self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.manager.public()['delivery']['status'], 'partial')

    def test_bounded_queue_and_forget_deleted_host(self):
        self.manager.jobs = queue.Queue(maxsize=1)
        self.manager.test()
        with self.assertRaises(ValueError):
            self.manager.test()
        self.observe('down', 'down')
        self.assertFalse(self.manager.records['one', 'host']['alert_pending'])
        self.assertEqual(self.manager.jobs.qsize(), 1)
        self.drain()
        self.observe('down')
        self.manager.forget('one')
        self.drain()
        self.assertEqual(self.send.call_count, 1)  # Only the queued test.

    def test_queue_overflow_retains_recovery_before_next_incident(self):
        self.observe('down', 'down')
        self.drain()
        self.manager.jobs = queue.Queue(maxsize=1)
        self.manager.test()  # Occupy the queue while recovery is confirmed.
        self.observe('up', 'up', 'down', 'down')
        self.assertIn('recovery_measurement', self.manager.records['one', 'host'])
        self.drain()  # Only the test mail can be delivered yet.
        self.observe('down')  # Recovery gets the slot; the new alert is deferred.
        self.drain()
        self.observe('down')
        self.drain()
        subjects = [call.args[1] for call in self.send.call_args_list if '測試' not in call.args[1]]
        self.assertEqual(len(subjects), 3)
        self.assertIn('主機異常', subjects[0])
        self.assertIn('主機恢復', subjects[1])
        self.assertIn('主機異常', subjects[2])

    def test_overflow_alert_keeps_original_abnormal_measurement(self):
        self.manager.jobs = queue.Queue(maxsize=1)
        self.manager.test()
        self.observe('down', 'down')
        self.drain()
        self.observe('up')  # One good sample is not yet a confirmed recovery.
        self.drain()
        self.assertIn('主機異常', self.send.call_args.args[1])
        self.assertIn('無法連線', self.send.call_args.args[2])
        self.assertNotIn('運作正常', self.send.call_args.args[2])

    def test_ambiguous_delivery_is_not_retried_and_recovery_explains_it(self):
        self.send.side_effect = mail.DeliveryUncertain()
        self.observe('down', 'down')
        self.drain()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.manager.public()['delivery']['status'], 'uncertain')
        self.send.side_effect = None
        self.send.return_value = True
        self.observe('down', 'up', 'up')
        self.drain()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('先前異常通知的 SMTP 接受結果未能確認', self.send.call_args.args[2])

    def test_noop_update_preserves_incident_and_queued_jobs(self):
        self.observe('down', 'down')
        generation = self.manager.generation
        self.manager.update({})
        self.assertEqual(self.manager.generation, generation)
        self.drain()
        self.assertEqual(self.send.call_count, 1)

    def test_host_header_and_url_secrets_do_not_enter_message(self):
        self.host['name'] = 'host\r\nBcc: attacker@example.com'
        last = {'status': 'degraded', 'timestamp': time.time(),
                'checks': [{'ok': False, 'type': 'http', 'label': 'http://private/?secret=sensitive'}]}
        self.manager.observe(self.host, last)
        self.manager.observe(self.host, last)
        self.drain()
        self.assertNotIn('\n', self.send.call_args.args[1])
        self.assertNotIn('sensitive', self.send.call_args.args[2])


class TransportTests(unittest.TestCase):
    def test_starttls_and_ssl_verify_and_authenticate(self):
        for mode, number, factory in [('starttls', 587, 'SMTP'), ('ssl', 465, 'SMTP_SSL')]:
            with self.subTest(mode=mode), patch.object(mail.smtplib, factory) as smtp:
                client = smtp.return_value
                client.send_message.return_value = {}
                self.assertTrue(mail.send_email(mail.validate_settings(config(security=mode, smtp_port=number)), 'subject', 'body'))
                client.connect.assert_called_once_with('smtp.gmail.com', number)
                self.assertEqual(client._host, 'smtp.gmail.com')
                if mode == 'starttls':
                    context = client.starttls.call_args.kwargs['context']
                    self.assertTrue(context.check_hostname)
                    calls = [call[0] for call in client.method_calls]
                    self.assertLess(calls.index('starttls'), calls.index('login'))
                else:
                    self.assertTrue(smtp.call_args.kwargs['context'].check_hostname)
                client.login.assert_called_once_with('sender@gmail.com', 'fake-app-password')
                message = client.send_message.call_args.args[0]
                self.assertNotIn('admin@example.com', str(message['To']))
                self.assertNotIn('fake-app-password', message.as_string())
                client.close.assert_called_once()

    def test_lost_final_smtp_ack_is_uncertain_not_retryable(self):
        with patch.object(mail.smtplib, 'SMTP') as smtp:
            client = smtp.return_value
            client.send_message.side_effect = mail.smtplib.SMTPServerDisconnected('lost final ack')
            with self.assertRaises(mail.DeliveryUncertain):
                mail.send_email(mail.validate_settings(config()), 'subject', 'body')
            client.send_message.assert_called_once()
            client.close.assert_called_once()

    def test_stalled_delivery_is_bounded_no_replacement_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = mail.NotificationManager(directory)
            manager.update(config(enabled=False))
            entered, release = threading.Event(), threading.Event()
            def stalled(*args):
                entered.set()
                release.wait(5)
                return True
            try:
                with patch.object(mail, 'send_email', side_effect=stalled) as send, \
                        patch.object(mail, 'DELIVERY_DEADLINE', 0.03):
                    manager.test()
                    self.assertTrue(entered.wait(1))
                    limit = time.monotonic() + 2
                    while manager.public()['delivery']['status'] != 'timeout' and time.monotonic() < limit:
                        time.sleep(0.01)
                    self.assertEqual(manager.public()['delivery']['status'], 'timeout')
                    manager.last_test = None
                    manager.test()
                    time.sleep(0.08)
                    self.assertEqual(send.call_count, 1)
                    started = time.monotonic()
                    manager.close()
                    self.assertLess(time.monotonic() - started, 0.8)
                    release.set()
                    manager.worker.join(timeout=1)
                    self.assertFalse(manager.worker.is_alive())
            finally:
                release.set()
                manager.close()


class MonitorHookTests(unittest.TestCase):
    def test_reconfigure_only_invalidates_affected_events(self):
        cases = (
            ({'docker_monitor_enabled': False}, {'網站異常'}),
            ({'website_monitor_enabled': False}, {'Docker 服務異常', '主機異常'}),
            ({'agent_port': 9106}, {'網站異常'}),
            ({'checks': [{'type': 'tcp', 'port': 22}]}, {'Docker 服務異常', '網站異常'}),
            ({'ip': '192.168.6.44'}, set()),
            (None, set()),
            ({'name': '新名稱'}, {'Docker 服務異常', '主機異常', '網站異常'}),
            ('save_error', {'Docker 服務異常', '主機異常', '網站異常'}),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory, \
                    patch.object(mail, 'send_email', return_value=True) as send:
                monitor = server.Monitor(directory)
                manager = monitor.notifications
                manager.update(config())
                manager.worker = Mock()
                config_data = {'ip': '192.168.6.23', 'agent_enabled': True, 'agent_token': 'x' * 24,
                               'docker_monitor_enabled': True, 'website_monitor_enabled': True}
                host = monitor.change(config_data)
                last = {'status': 'degraded', 'host_status': 'degraded', 'timestamp': time.time(),
                        'agent': {'ok': True, 'metrics': {'services': [
                            {'name': 'docker.service', 'status': 'failed'}, {'name': 'ssh', 'status': 'failed'}]}},
                        'docker': {'ok': False, 'code': 'failed'},
                        'website': {'ok': False, 'code': 'connection'}}
                for _ in range(2):
                    manager.observe(host, last)
                if changes == 'save_error':
                    with patch.object(monitor, 'save', side_effect=OSError), self.assertRaises(OSError):
                        monitor.change(dict(config_data, docker_monitor_enabled=False), host['id'])
                elif changes is None:
                    monitor.change({}, host['id'], delete=True)
                else:
                    monitor.change(dict(config_data, **changes), host['id'])
                while not manager.jobs.empty():
                    manager._process(manager.jobs.get_nowait())
                    manager.jobs.task_done()
                self.assertEqual({call.args[1].split('] ')[1].split('：')[0] for call in send.call_args_list}, expected)
                for call in send.call_args_list:
                    if '主機異常' in call.args[1]:
                        self.assertIn('ssh', call.args[2])
                        self.assertNotIn('docker.service', call.args[2])
                manager.close()

    def test_only_committed_measurements_notify_and_cosmetic_edits_preserve(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = server.Monitor(directory)
            host = monitor.change({'ip': '127.0.0.1'})
            last = {'status': 'down', 'timestamp': time.time(), 'latency_ms': None}
            with patch.object(server, 'inspect_host', return_value=last), \
                    patch.object(monitor.notifications, 'observe') as observe:
                monitor.cycle()
                observe.assert_called_once()
                monitor.state()
                self.assertEqual(observe.call_count, 1)
            with patch.object(monitor.notifications, 'forget') as forget:
                monitor.change({'ip': '127.0.0.1', 'name': 'renamed'}, host['id'])
                forget.assert_not_called()
                monitor.change({}, host['id'], delete=True)
                forget.assert_called_once_with(host['id'])


if __name__ == '__main__':
    unittest.main()
