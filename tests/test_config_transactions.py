import copy
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import server
from test_config_import import export, snapshot


class ImportTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.monitor = server.Monitor(self.directory.name)
        self.addCleanup(self.monitor.close)
        self.host = self.monitor.change({'ip': '127.0.0.1', 'agent_enabled': True, 'agent_token': 'w' * 24})
        self.monitor.update_settings({'refresh_interval': 5})

    def preview(self, document=None):
        return self.monitor.preview_import(export() if document is None else document)['confirmation_token']

    def test_apply_is_atomic_and_persists_after_restart(self):
        m = self.monitor
        m.update_notifications({'username': 'test@example.com', 'password': 'local-password'})
        directory = Path(self.directory.name)
        legacy = {path.name: path.read_bytes() for path in directory.glob('*.json')}
        before = copy.deepcopy(snapshot(m))
        generation = m.notifications.generation
        code = self.preview(export([{'ip': '127.0.0.2'}], smtp_host='smtp.example.com'))
        self.assertEqual(before, snapshot(m))
        self.assertFalse((directory / 'configuration.json').exists())
        with patch('server.os.replace', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            m.apply_import(code)
        self.assertEqual(before, snapshot(m))
        self.assertEqual(generation, m.notifications.generation)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, legacy)
        response = m.apply_import(code)
        self.assertEqual(response, {'ok': True, 'settings': {'refresh_interval': 6, 'monitor_interval': 33}})
        path = directory / 'configuration.json'
        self.assertTrue(path.exists())
        self.assertEqual([host['ip'] for host in m.hosts], ['127.0.0.2'])
        self.assertEqual(m.notifications.settings['password'], '')
        self.assertGreater(m.notifications.generation, generation)
        self.assertEqual({name: (directory / name).read_bytes() for name in legacy}, legacy)
        reloaded = server.Monitor(directory)
        self.addCleanup(reloaded.notifications.close)
        self.assertEqual(snapshot(reloaded), snapshot(m))

        reloaded.change({'ip': '127.0.0.3'})
        reloaded.update_settings({'monitor_interval': 88})
        reloaded.update_notifications({'username': 'm@example.com', 'password': 'new-password'})
        earlier = copy.deepcopy(snapshot(reloaded))
        on_disk = path.read_bytes()
        with patch('server.os.replace', side_effect=OSError('disk failure')):
            for write in (lambda: reloaded.change({'ip': '127.0.0.4'}),
                          lambda: reloaded.update_settings({'monitor_interval': 89}),
                          lambda: reloaded.update_notifications({'password': 'other'})):
                with self.assertRaises(OSError):
                    write()
                self.assertEqual(snapshot(reloaded), earlier)
                self.assertEqual(path.read_bytes(), on_disk)
        restart = server.Monitor(directory)
        self.addCleanup(restart.notifications.close)
        self.assertEqual(snapshot(restart), earlier)
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            server.Monitor(directory)

    def test_preview_revision_expiry_and_single_use(self):
        m = self.monitor
        a, b = self.preview(), self.preview()
        with self.assertRaises(server.ImportConflict):
            m.apply_import(a)
        m.update_settings({'refresh_interval': m.settings['refresh_interval']})
        self.assertTrue(m.state()['hosts'])
        m.apply_import(b)
        with self.assertRaises(server.ImportConflict):
            m.apply_import(b)

        m.change({'ip': '127.0.0.1', 'agent_enabled': True, 'agent_token': 'w' * 24})
        code = self.preview()
        identity = m.hosts[0]['id']
        options = {'ip': '127.0.0.1', 'agent_enabled': True}
        m.change(dict(options, agent_token='x' * 24), identity)
        m.change(dict(options, agent_token='w' * 24), identity)
        with self.assertRaises(server.ImportConflict):
            m.apply_import(code)
        code = self.preview()
        m.update_notifications({'password': 'changed-only-secret'})
        with self.assertRaises(server.ImportConflict):
            m.apply_import(code)
        with patch('server.time.monotonic', return_value=1000):
            code = self.preview()
        with patch('server.time.monotonic', return_value=1600), self.assertRaises(server.ImportConflict):
            m.apply_import(code)

        code = self.preview()
        def confirm():
            try:
                return m.apply_import(code)['ok']
            except server.ImportConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: confirm(), range(2))), [False, True])

    def test_private_configuration_rejects_missing_fields_or_duplicate_keys(self):
        m = self.monitor
        m.apply_import(self.preview(m.export_config()))
        private_path = m.configuration_path
        pristine = json.loads(private_path.read_text(encoding='utf-8'))
        for key, section in (('agent_port', 'hosts'), ('agent_token', 'hosts'),
                             ('smtp_host', 'notifications'), ('password', 'notifications'),
                             ('monitor_interval', 'settings')):
            with self.subTest(key=key):
                broken = copy.deepcopy(pristine)
                record = broken[section][0] if section == 'hosts' else broken[section]
                del record[key]
                private_path.write_text(json.dumps(broken), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, r'configuration\.json'):
                    server.Monitor(self.directory.name)
        duplicate = json.dumps(pristine).replace('"agent_port": 9105', '"agent_port": 9105, "agent_port": 9106')
        private_path.write_text(duplicate, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, r'configuration\.json'):
            server.Monitor(self.directory.name)

    def test_import_cancels_mail_that_has_not_started_transport(self):
        m = self.monitor
        notice = m.notifications
        notice.worker = Mock()
        m.update_notifications({'smtp_host': 'mail.example.com', 'sender': 'test@example.com',
                                'recipients': ['receiver@example.com']})
        notice.test()
        job = notice.jobs.get_nowait()
        original_deliver = notice._deliver
        code = self.preview()

        def committed_before_transport(*args):
            m.apply_import(code)
            return original_deliver(*args)

        with patch.object(notice, '_deliver', side_effect=committed_before_transport), \
                patch('notifications.send_email') as send:
            notice._process(job)
        send.assert_not_called()
        self.assertEqual(notice.public()['delivery']['status'], 'idle')

    def test_preview_releases_candidate_after_deadline_without_apply(self):
        m = self.monitor
        expired = threading.Event()
        original = m._expire_preview
        def expiry():
            original()
            if m.pending_import is None:
                expired.set()
        with patch.object(server, 'PREVIEW_TTL', 0.05), patch.object(m, '_expire_preview', side_effect=expiry):
            code = self.preview()
            self.assertIsNotNone(m.pending_import)
            self.assertTrue(expired.wait(2))
            self.assertIsNone(m.pending_import)
        with self.assertRaises(server.ImportConflict):
            m.apply_import(code)

    def test_import_discards_stale_measurement_and_email_job(self):
        m = self.monitor
        m.update_notifications({'enabled': True, 'smtp_host': 'mail.example.com',
                                'sender': 'test@example.com', 'recipients': ['receiver@example.com'],
                                'failure_threshold': 1})
        m.notifications.worker = Mock()
        m.notifications.observe(m.hosts[0], {'status': 'down', 'timestamp': time.time()})
        pending = m.notifications.jobs.get_nowait()
        entered, release = threading.Event(), threading.Event()
        def inspect(host):
            entered.set()
            release.wait(3)
            return {'status': 'down', 'timestamp': time.time(), 'latency_ms': None}
        with patch.object(server, 'inspect_host', side_effect=inspect):
            thread = threading.Thread(target=m.cycle)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                m.wake.clear()
                m.apply_import(self.preview(m.export_config()))
                self.assertTrue(m.wake.is_set())
            finally:
                release.set()
                thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertIsNone(m.state()['hosts'][0]['last'])
        with patch('notifications.send_email') as send:
            m.notifications._process(pending)
            send.assert_not_called()
