import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import Mock, patch

import server


def valid_metrics():
    return {'hostname': 'host', 'os': 'Linux', 'cpu_percent': 10,
            'memory': {'percent': 50, 'total': 1000, 'used': 500},
            'disks': [{'mountpoint': '/', 'percent': 50, 'total': 1000, 'used': 500}],
            'uptime_seconds': 60, 'services': [{'name': 'ssh', 'status': 'active'}]}


class ValidationTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(server.scan_addresses('192.168.1.0/30'), ['192.168.1.1', '192.168.1.2'])
        self.assertEqual(server.scan_addresses('10.0.0.1-10.0.0.2'), ['10.0.0.1', '10.0.0.2'])
        for invalid in ['8.8.8.8', '10.0.0.0/16', '192.168.1.9-192.168.1.1', 'localhost', '169.254.0.1']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server.scan_addresses(invalid)

    def test_host_validation(self):
        base = {'ip': '192.168.1.1', 'checks': [{'type': 'http', 'url': 'http://192.168.1.1/health'}]}
        self.assertEqual(server.validate_host(base)['ip'], base['ip'])
        for check in [{'type': 'tcp', 'port': True}, {'type': 'tcp', 'port': 0}, {'type': 'http', 'url': 'http://example.com'}, {'type': 'http', 'url': 'http://192.168.1.1:99999'}]:
            with self.subTest(check=check), self.assertRaises(ValueError):
                server.validate_host({**base, 'checks': [check]})
        with self.assertRaises(ValueError):
            server.validate_host({**base, 'agent_enabled': True})

    def test_metrics_required_fields_and_ranges(self):
        original = valid_metrics()
        self.assertEqual(server.validate_metrics(original), original)
        bad = [{}, {'memory': {}, 'disks': [{}]}]
        for key in original:
            item = valid_metrics()
            del item[key]
            bad.append(item)
        for path, value in [(('cpu_percent',), -100), (('cpu_percent',), 101),
                            (('uptime_seconds',), -1), (('cpu_percent',), True),
                            (('memory', 'percent'), 101), (('memory', 'used'), -1),
                            (('memory', 'total'), None)]:
            item = valid_metrics()
            target = item if len(path) == 1 else item[path[0]]
            target[path[-1]] = value
            bad.append(item)
        for key in ('disks', 'services'):
            bad.append({**valid_metrics(), key: [{}]})
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                server.validate_metrics(item)

    def test_token_preservation(self):
        host = server.validate_host({'ip': '10.0.0.1', 'agent_enabled': True, 'agent_token': 'x' * 24})
        edited = server.validate_host({'ip': '10.0.0.1', 'agent_enabled': True}, host)
        self.assertEqual(edited['agent_token'], host['agent_token'])


class MonitorTests(unittest.TestCase):
    def test_persistence_public_state_and_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = server.Monitor(directory)
            host = monitor.change({'ip': '10.1.1.1', 'agent_token': 'x' * 24})
            self.assertNotIn('agent_token', host)
            self.assertNotIn('agent_token', monitor.state()['hosts'][0])
            self.assertEqual(len(server.Monitor(directory).hosts), 1)
            with self.assertRaises(ValueError):
                monitor.change({'ip': '10.1.1.1'})
            monitor.change({}, host['id'], delete=True)
            self.assertEqual(monitor.state()['hosts'], [])

    def test_private_temporary_file_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = server.Monitor(directory)
            replace = os.replace
            def checked_replace(source, destination):
                self.assertNotEqual(Path(source).name, 'hosts.tmp')
                if os.name == 'posix':
                    self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                replace(source, destination)
            with patch('server.os.replace', side_effect=checked_replace):
                monitor.change({'ip': '127.0.0.1'})
            before = monitor.path.read_bytes()
            with patch('server.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    monitor.change({'ip': '127.0.0.2'})
            self.assertEqual(monitor.path.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            self.assertEqual(len(monitor.hosts), 1)

    @patch('server.ping', return_value=(False, None))
    @patch('server.tcp')
    def test_blocked_icmp_with_working_tcp(self, tcp, ping):
        host = server.validate_host({'ip': '10.0.0.1', 'checks': [{'type': 'tcp', 'port': 22}]})
        self.assertEqual(server.inspect_host(host)['status'], 'up')
        tcp.side_effect = OSError('connection refused')
        self.assertEqual(server.inspect_host(host)['status'], 'down')

    @patch('server.ping', return_value=(True, 2))
    @patch('server.http_get', return_value=(503, b''))
    def test_http_error_degrades(self, request, ping):
        host = server.validate_host({'ip': '10.0.0.1', 'checks': [{'type': 'http', 'url': 'http://10.0.0.1/'}]})
        self.assertEqual(server.inspect_host(host)['status'], 'degraded')

    @patch('server.ping', return_value=(False, None))
    def test_service_failure(self, ping):
        host = server.validate_host({'ip': '10.0.0.1', 'agent_enabled': True, 'agent_token': 'x' * 24})
        for status in ('active', 'inactive'):
            metrics = valid_metrics()
            metrics['services'][0]['status'] = status
            with patch('server.http_get', return_value=(200, json.dumps(metrics).encode())):
                result = server.inspect_host(host)
            self.assertTrue(result['agent']['ok'])
            self.assertEqual(result['status'], 'up' if status == 'active' else 'degraded')

    @patch('server.ping', return_value=(False, None))
    @patch('server.http_get', return_value=(200, b'{"cpu_percent":NaN}'))
    def test_invalid_agent_json(self, request, ping):
        host = server.validate_host({'ip': '10.0.0.1', 'agent_enabled': True, 'agent_token': 'x' * 24})
        self.assertFalse(server.inspect_host(host)['agent']['ok'])

    def test_save_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = server.Monitor(directory)
            with patch.object(monitor, 'save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    monitor.change({'ip': '127.0.0.1'})
            self.assertEqual(monitor.hosts, [])

    @patch('server.ping', return_value=(False, None))
    def test_bad_agent_payload_does_not_break_state(self, ping):
        host = server.validate_host({'ip': '127.0.0.1', 'agent_enabled': True, 'agent_token': 'x' * 24})
        for payload in [b'{"cpu_percent":1e999}', b'{"hostname":"\\ud800"}', b'{"disks":[null]}', b'{"services":[null]}', b'{"memory":[]}']:
            with patch('server.http_get', return_value=(200, payload)):
                result = server.inspect_host(host)
            self.assertFalse(result['agent']['ok'])
            self.assertEqual(result['status'], 'degraded')
            json.dumps(result, allow_nan=False, ensure_ascii=False).encode('utf-8')

    @patch('server.ping', return_value=(False, None))
    @patch('server.http_get', return_value=(401, b''))
    def test_agent_auth_error_is_degraded(self, request, ping):
        host = server.validate_host({'ip': '127.0.0.1', 'agent_enabled': True, 'agent_token': 'x' * 24})
        self.assertEqual(server.inspect_host(host)['status'], 'degraded')

    @patch('server.inspect_host')
    def test_history_bounded(self, inspect):
        inspect.return_value = {'timestamp': 1, 'status': 'up', 'latency_ms': 2}
        with tempfile.TemporaryDirectory() as directory:
            monitor = server.Monitor(directory)
            monitor.change({'ip': '127.0.0.1'})
            for _ in range(122):
                monitor.cycle()
            self.assertEqual(len(monitor.state()['hosts'][0]['history']), 120)


class HTTPProbeTests(unittest.TestCase):
    def test_unicode_path_encoding(self):
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.seen = self.path
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        endpoint = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(server.http_get('127.0.0.1', endpoint.server_port, '/健康?q=正常')[0], 200)
            self.assertEqual(endpoint.seen, '/%E5%81%A5%E5%BA%B7?q=%E6%AD%A3%E5%B8%B8')
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            thread.join()

    def test_absolute_response_deadline(self):
        class SlowEndpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '1000')
                self.end_headers()
                try:
                    for _ in range(1000):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        endpoint = ThreadingHTTPServer(('127.0.0.1', 0), SlowEndpoint)
        thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            try:
                server.http_get('127.0.0.1', endpoint.server_port, '/metrics', token='x' * 24)
            except (OSError, http.client.HTTPException):
                pass
            self.assertLess(time.monotonic() - started, 7)
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            thread.join()


class APITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = server.Server(('127.0.0.1', 0), server.Handler)
        self.server.monitor = server.Monitor(self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        hdr = {'Content-Type': 'application/json'}
        hdr.update(headers or {})
        connection.request(method, path, json.dumps(data) if data is not None else None, hdr)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, json.loads(body)

    def test_api_crud(self):
        status, host = self.request('POST', '/api/hosts', {'ip': '127.0.0.1'})
        self.assertEqual(status, 201)
        self.assertEqual(self.request('GET', '/api/state')[1]['hosts'][0]['ip'], '127.0.0.1')
        self.assertEqual(self.request('PUT', '/api/hosts/' + host['id'], {'ip': '127.0.0.1', 'name': 'test'})[0], 200)
        self.assertEqual(self.request('DELETE', '/api/hosts/' + host['id'])[0], 200)
        self.assertEqual(self.request('GET', '/api/state')[1]['hosts'], [])

    def test_notification_api_secrets_persistence_and_origin_protection(self):
        settings = {'username': 'sample@gmail.com', 'sender': 'sample@gmail.com',
                    'recipients': ['admin@example.com'], 'password': 'fake-test-password'}
        status, public = self.request('PUT', '/api/notifications', settings)
        self.assertEqual(status, 200)
        self.assertTrue(public['password_set'])
        self.assertNotIn('fake-test-password', json.dumps(public))
        public = self.request('GET', '/api/notifications')[1]
        self.assertNotIn('password', public)
        self.assertEqual(self.request('PUT', '/api/notifications', {'notify_recovery': False})[0], 200)
        self.assertEqual(self.server.monitor.notifications.settings['password'], 'fake-test-password')
        self.assertEqual(self.request('POST', '/api/notifications/test', {},
                                     {'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.request('PUT', '/api/notifications', {'security': 'none'})[0], 400)
        with patch.object(self.server.monitor.notifications, '_enqueue', return_value=True):
            self.assertEqual(self.request('POST', '/api/notifications/test', {})[0], 202)
            self.assertEqual(self.request('POST', '/api/notifications/test', {})[0], 400)
        self.assertEqual(self.request('PUT', '/api/notifications', {'password': ''})[0], 200)
        self.assertFalse(self.request('GET', '/api/notifications')[1]['password_set'])

    def test_absolute_request_deadline(self):
        self.server.request_deadline = 0.2
        with patch.object(self.server, 'handle_error'):
            for partial in (b'GET /api/state HTTP/1.1\r\nX-Test: ',
                            (f'POST /api/hosts HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'
                             'Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{').encode()):
                connection = socket.create_connection(self.server.server_address, timeout=2)
                started = time.monotonic()
                try:
                    connection.sendall(partial)
                    for _ in range(20):
                        time.sleep(0.04)
                        try:
                            connection.sendall(b' ')
                        except OSError:
                            break
                    while connection.recv(4096):
                        pass
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass
                finally:
                    connection.close()
                self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.server.monitor.hosts, [])
        self.assertEqual(self.request('GET', '/api/state')[0], 200)

    def test_connection_limit(self):
        for _ in range(server.MAX_CLIENTS):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        try:
            request = Mock()
            self.server.process_request(request, ('127.0.0.1', 1))
            request.close.assert_called_once()
        finally:
            for _ in range(server.MAX_CLIENTS):
                self.server.slots.release()
        self.assertEqual(self.request('GET', '/api/state')[0], 200)

    def test_reject_cross_origin_and_bad_hosts(self):
        self.assertEqual(self.request('POST', '/api/refresh', {}, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/state', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/hosts', {'ip': '8.8.8.8'})[0], 400)
        self.assertEqual(self.request('POST', '/api/hosts', [1])[0], 400)


if __name__ == '__main__':
    unittest.main()
