import io
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import website_probe as probe


URC = '192.168.6.23'
AI = '192.168.6.44'
DOMAIN = 'urc.landagent.com.tw'


def address(ip):
    ipv6 = ':' in ip
    return (socket.AF_INET6 if ipv6 else socket.AF_INET, socket.SOCK_STREAM,
            socket.IPPROTO_TCP, '', (ip, 443, 0, 0) if ipv6 else (ip, 443))


class WebsiteProbeTests(unittest.TestCase):
    def test_public_addresses_and_tls_hostname(self):
        fake_socket = MagicMock()
        fake_socket.__enter__.return_value = fake_socket
        tls = Mock()
        transport = MagicMock()
        transport.__enter__.return_value = transport
        response = MagicMock(wraps=io.BytesIO(b'HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n'))
        response.__enter__.return_value = response
        transport.makefile.return_value = response
        tls.wrap_socket.return_value = transport
        resolver_answers = [address(ip) for ip in (
            '127.0.0.1', '10.0.0.1', '100.64.0.1', 'fe80::1', '224.0.0.1',
            '240.0.0.1', '::ffff:8.8.8.8', '8.8.8.8', '2606:4700::1111')]
        with patch.object(probe.socket, 'getaddrinfo', return_value=resolver_answers) as resolve, \
                patch.object(probe.socket, 'socket', return_value=fake_socket), \
                patch.object(probe.ssl, 'create_default_context', return_value=tls):
            self.assertIs(probe.probe_website(URC)['ok'], True)
            resolve.assert_called_once()
            self.assertEqual(resolve.call_args.args[:2], (DOMAIN, 443))
            fake_socket.connect.assert_called_once_with(('8.8.8.8', 443))
            tls.wrap_socket.assert_called_once_with(fake_socket, server_hostname=DOMAIN,
                                                   do_handshake_on_connect=False)
            request = tls.wrap_socket.return_value.sendall.call_args.args[0]
            self.assertTrue(request.startswith(b'GET / HTTP/1.1\r\n'))
            self.assertIn(b'Host: urc.landagent.com.tw\r\n', request)
            self.assertNotIn(b'Authorization', request)
            response.read.assert_not_called()
            resolve.return_value = resolver_answers[:-2]
            fake_socket.reset_mock()
            result = probe.probe_website(URC)
            self.assertEqual((result['code'], result['ok']), ('dns', False))
            fake_socket.connect.assert_not_called()
            with self.assertRaises(ValueError):
                probe.probe_website('192.168.6.24')

    def test_http_classification_and_fallback(self):
        for status, ok in ((204, True), (302, False), (404, False), (503, False)):
            with self.subTest(status=status), \
                    patch.object(probe.socket, 'getaddrinfo', return_value=[address('8.8.8.8'), address('1.1.1.1')]), \
                    patch.object(probe, '_request_status', return_value=status) as request:
                result = probe.probe_website(URC)
                self.assertEqual((result['code'], result['http_status'], result['ok']), ('http', status, ok))
                request.assert_called_once()
        with patch.object(probe.socket, 'getaddrinfo', return_value=[address('8.8.8.8'), address('1.1.1.1')]), \
                patch.object(probe, '_request_status', side_effect=[OSError('raw secret'), 200]) as request:
            self.assertTrue(probe.probe_website(URC)['ok'])
            self.assertEqual(request.call_count, 2)
            for error, code in ((ssl.SSLCertVerificationError('raw secret'), 'certificate'),
                                (OSError('raw secret'), 'connection'), (TimeoutError('raw secret'), 'timeout')):
                request.side_effect = error
                self.assertEqual(probe.probe_website(URC),
                                 {'code': code, 'ok': False, 'latency_ms': None})
        with patch.object(probe.socket, 'getaddrinfo', side_effect=socket.gaierror('raw secret')):
            self.assertEqual(probe.probe_website(URC)['code'], 'dns')

    def test_deadline_and_per_site_busy(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def resolve(domain, *_args, **_kwargs):
            if domain == DOMAIN:
                entered.set()
                release.wait(2)
                finished.set()
            return [address('8.8.8.8')]

        with patch.object(probe, 'DEADLINE', 0.15), \
                patch.object(probe.socket, 'getaddrinfo', side_effect=resolve), \
                patch.object(probe, '_request_status', return_value=200) as request:
            try:
                started = time.monotonic()
                self.assertEqual(probe.probe_website(URC)['code'], 'timeout')
                self.assertLess(time.monotonic() - started, 0.9)
                self.assertTrue(entered.is_set())
                result = probe.probe_website(URC)
                self.assertEqual((result['code'], result['ok']), ('busy', None))
                self.assertTrue(probe.probe_website(AI)['ok'])
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                self.assertTrue(probe._SLOTS[DOMAIN].acquire(timeout=1))
                probe._SLOTS[DOMAIN].release()
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0], 'ai.tailan.com.tw')

    def test_header_deadline_closes_transport(self):
        # Local socket pair stands in for the decrypted TLS stream. A peer that
        # never finishes its headers must see EOF and release the worker slot.
        local, peer = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(peer.close)
        raw = MagicMock()
        raw.__enter__.return_value = raw
        tls = Mock()
        transport = MagicMock(wraps=local)
        transport.__enter__.return_value = transport
        transport.__exit__.side_effect = lambda *_args: local.close()
        transport.do_handshake = Mock()
        tls.wrap_socket.return_value = transport
        peer.settimeout(2)
        peer.sendall(b'HTTP/1.1 200 OK\r\nX-Unfinished: ')
        with patch.object(probe, 'DEADLINE', 0.2), \
                patch.object(probe.socket, 'getaddrinfo', return_value=[address('8.8.8.8')]), \
                patch.object(probe.socket, 'socket', return_value=raw), \
                patch.object(probe.ssl, 'create_default_context', return_value=tls):
            self.assertEqual(probe.probe_website(AI)['code'], 'timeout')
            data = bytearray()
            while chunk := peer.recv(4096):
                data.extend(chunk)
            self.assertIn(b'GET / HTTP/1.1', data)
            self.assertTrue(probe._SLOTS['ai.tailan.com.tw'].acquire(timeout=1))
            probe._SLOTS['ai.tailan.com.tw'].release()

    def test_rejects_incomplete_headers_and_waits_for_final_status(self):
        for wire, expected in (
            (b'HTTP/1.1 200 OK\r\nX-Header: incomplete', ('connection', False, None)),
            (b'HTTP/1.1 200 OK\r\nX-Header: complete line\r\n', ('connection', False, None)),
            (b'HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r\n'
             b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n', ('http', True, 200)),
            (b'HTTP/1.1 103 Early Hints\r\n\r\n'
             b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n', ('http', False, 503)),
        ):
            with self.subTest(wire=wire), \
                    patch.object(probe.socket, 'getaddrinfo', return_value=[address('8.8.8.8')]), \
                    patch.object(probe.socket, 'socket'), \
                    patch.object(probe.ssl, 'create_default_context') as context:
                transport = context.return_value.wrap_socket.return_value.__enter__.return_value
                transport.makefile.return_value = io.BytesIO(wire)
                result = probe.probe_website(URC)
                self.assertEqual((result['code'], result['ok'], result.get('http_status')), expected)
