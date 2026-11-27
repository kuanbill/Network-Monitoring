"""Desktop tests always use temporary settings and an isolated loopback port."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import urllib.request

import server
import windows_app as app


@unittest.skipUnless(sys.platform == 'win32', 'Windows desktop only')
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        (self.directory / 'data').mkdir()
        (self.directory / 'logs').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def test_data_path_is_not_source_or_install_directory(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name}):
            self.assertEqual(app.app_directory(), self.directory / 'NetwordMonitor')

    def test_single_instance_lock_is_released(self):
        first = app.InstanceLock(self.directory)
        try:
            with self.assertRaises(RuntimeError):
                app.InstanceLock(self.directory)
        finally:
            first.close()
        second = app.InstanceLock(self.directory)
        second.close()

    def test_named_events_are_shared_and_reset_for_each_start(self):
        event = app.NamedEvent()
        other = app.NamedEvent(event.name)
        try:
            self.assertFalse(event.wait(0))
            other.set()
            self.assertTrue(event.wait(100))
        finally:
            other.close()
            event.close()

    def test_backend_start_stop_restart_and_configuration_persistence(self):
        controller = app.BackendController(self.directory, self.free_port())
        url = controller.url
        try:
            controller.start()
            first_pid = controller.process.pid
            with urllib.request.urlopen(url + '/') as response:
                self.assertIn('notification-dialog', response.read().decode())
            payload = json.dumps({'name': 'Desktop test', 'ip': '127.0.0.1', 'checks': []}).encode()
            request = urllib.request.Request(url + '/api/hosts', data=payload,
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 201)
            saved = (self.directory / 'data' / 'hosts.json').read_bytes()
            settings = {'refresh_interval': 2, 'monitor_interval': 60}
            request = urllib.request.Request(url + '/api/settings', data=json.dumps(settings).encode(),
                                             headers={'Content-Type': 'application/json'}, method='PUT')
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response), settings)
            controller.start()
            self.assertEqual(controller.process.pid, first_pid)
            controller.stop()
            self.assertFalse(controller.running())
            controller.start()
            with urllib.request.urlopen(url + '/api/state') as response:
                self.assertEqual(len(json.load(response)['hosts']), 1)
            self.assertEqual((self.directory / 'data' / 'hosts.json').read_bytes(), saved)
            with urllib.request.urlopen(url + '/api/settings') as response:
                self.assertEqual(json.load(response), settings)
        finally:
            controller.stop()
        self.assertTrue((self.directory / 'logs' / 'application.log').exists())

    def test_occupied_port_never_opens_other_application(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            controller = app.BackendController(self.directory, occupied.getsockname()[1])
            with self.assertRaises(RuntimeError):
                controller.start()
            self.assertFalse(controller.running())
            controller.stop()
        self.assertFalse((self.directory / 'data' / 'hosts.json').exists())

    def test_desktop_loads_offline_monitor_flags_without_unscoped_api(self):
        port = self.free_port()
        session = 'offline-flags'
        base = f'http://127.0.0.1:{port}'
        stop, ready = app.NamedEvent(), app.NamedEvent()
        # Equivalent to a local edit while the desktop backend is stopped.
        saved = [{'ip': '192.168.6.23', 'agent_enabled': True, 'agent_token': 'x' * 24,
                  'docker_monitor_enabled': True, 'website_monitor_enabled': True}]
        (self.directory / 'data' / 'hosts.json').write_text(json.dumps(saved), encoding='utf-8')
        with patch.object(server.Monitor, 'loop'), patch.object(app, 'configure_logging'):
            backend = threading.Thread(target=app.run_backend,
                                       args=(self.directory, stop.name, ready.name, port, 0, session),
                                       daemon=True)
            backend.start()
            try:
                self.assertTrue(ready.wait(3000))
                with self.assertRaises(HTTPError) as denied:
                    urllib.request.urlopen(base + '/api/state', timeout=2)
                self.assertEqual(denied.exception.code, 404)
                denied.exception.close()
                with urllib.request.urlopen(f'{base}/__desktop__/{session}/api/state', timeout=2) as response:
                    host = json.load(response)['hosts'][0]
                self.assertTrue(host['docker_monitor_enabled'])
                self.assertTrue(host['website_monitor_enabled'])
                self.assertTrue(host['agent_token_set'])
                self.assertNotIn('agent_token', host)
            finally:
                stop.set()
                backend.join(4)
                stop.close()
                ready.close()
            self.assertFalse(backend.is_alive())

    def test_desktop_import_is_scoped_to_live_session(self):
        controller = app.BackendController(self.directory, self.free_port())
        try:
            controller.start()
            base = controller.url
            with urllib.request.urlopen(base + '/api/config/export', timeout=3) as reply:
                raw = reply.read()
            def post(url, body):
                request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=3) as reply:
                        return reply.status, json.load(reply)
                except HTTPError as reply:
                    with reply:
                        return reply.code, json.load(reply)
            path = '/api/config/import/preview'
            apply = '/api/config/import/apply'
            unscoped = base.split('/__desktop__', 1)[0]
            self.assertEqual(post(unscoped + path, raw)[0], 404)
            status, result = post(base + path, raw)
            self.assertEqual(status, 200)
            confirmation = json.dumps({'confirmation_token': result['confirmation_token']}).encode()
            self.assertEqual(post(unscoped + apply, confirmation)[0], 404)
            self.assertEqual(post(unscoped + '/__desktop__/stale-session' + apply, confirmation)[0], 404)
            self.assertFalse((self.directory / 'data' / 'configuration.json').exists())
            self.assertEqual(post(base + apply, confirmation)[0], 200)
            self.assertTrue((self.directory / 'data' / 'configuration.json').exists())
        finally:
            controller.stop()

    @unittest.skipUnless(importlib.util.find_spec('webview'), 'Install requirements-windows.txt for desktop test')
    def test_desktop_close_hides_and_tray_reopens_then_quits(self):
        import pystray
        import webview
        controller = Mock()
        controller.running.return_value = True
        controller.url = app.URL + '/__desktop__/test'
        window = Mock()
        closing = []
        class ClosingEvent:
            def __iadd__(self, callback):
                closing.append(callback)
                return self
        window.events.closing = ClosingEvent()
        icons = []
        def tray_loop(icon):
            icons.append(icon)
        def ui_loop(**kwargs):
            self.assertEqual(kwargs, dict(gui='edgechromium', debug=False, private_mode=True))
            self.assertFalse(closing[0]())
            window.hide.assert_called_once()
            items = list(icons[0].menu)
            self.assertIn('啟動監控', [item.text for item in items])
            self.assertIn('停止監控', [item.text for item in items])
            items[0](icons[0])
            window.show.assert_called_once()
            window.restore.assert_called_once()
            items[-1](icons[0])
            window.destroy.assert_called_once()
            self.assertIsNone(closing[0]())
        with patch.object(app, 'BackendController', return_value=controller), \
                patch.object(app, 'require_webview2'), \
                patch.object(pystray.Icon, 'run_detached', tray_loop), \
                patch.object(pystray.Icon, 'stop'), \
                patch.object(webview, 'create_window', return_value=window) as create, \
                patch.object(webview, 'start', side_effect=ui_loop), \
                patch('webbrowser.open') as browser:
            app.run_tray(self.directory)
            controller.start.assert_called_once()
            controller.stop.assert_called_once()
            self.assertEqual(create.call_args.args, (app.APP_NAME, controller.url + '/'))
            browser.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('webview'), 'Install requirements-windows.txt for desktop test')
    def test_failed_backend_never_creates_window(self):
        import webview
        controller = Mock()
        controller.start.side_effect = RuntimeError('occupied')
        controller.running.return_value = False
        with patch.object(app, 'BackendController', return_value=controller), \
                patch.object(app, 'message'), patch.object(app, 'require_webview2'), \
                patch.object(webview, 'create_window') as create:
            app.run_tray(self.directory)
            create.assert_not_called()
            controller.stop.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec('webview'), 'Install requirements-windows.txt for desktop test')
    def test_missing_runtime_stops_backend_and_explains(self):
        import webview
        controller = Mock()
        controller.running.return_value = True
        controller.url = app.URL + '/__desktop__/test'
        with patch.object(app, 'BackendController', return_value=controller), \
                patch.object(app, 'require_webview2'), \
                patch('pystray.Icon.run_detached'), patch('pystray.Icon.stop'), \
                patch.object(webview, 'create_window'), \
                patch.object(webview, 'start', side_effect=RuntimeError('missing runtime')), \
                patch.object(app, 'message') as message:
            with self.assertRaises(RuntimeError):
                app.run_tray(self.directory)
            self.assertIn('WebView2 Runtime', message.call_args.args[0])
            controller.stop.assert_called_once()

    def test_desktop_session_paths_cannot_target_replacement_backend(self):
        first = app.BackendController(self.directory, self.free_port())
        second = app.BackendController(self.directory, first.port)
        try:
            first.start()
            with urllib.request.urlopen(first.url + '/static/app.js') as response:
                self.assertIn(first.session.encode(), response.read())
            first.stop()
            second.start()
            for path in ('/', '/static/app.js', '/static/style.css'):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(first.url + path)
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
            for method in ('GET', 'POST', 'PUT', 'DELETE'):
                request = urllib.request.Request(first.url + '/api/hosts', method=method,
                                                 data=None if method == 'GET' else b'{}',
                                                 headers={'Content-Type': 'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
            self.assertFalse((self.directory / 'data' / 'hosts.json').exists())
        finally:
            first.stop()
            second.stop()

    def test_webview2_preflight_rejects_legacy_renderer(self):
        gui = Mock()
        gui.initialize.return_value.renderer = 'mshtml'
        with patch.dict(sys.modules, {'webview.guilib': gui}):
            with self.assertRaisesRegex(RuntimeError, 'WebView2 Runtime required'):
                app.require_webview2()
        gui.initialize.assert_called_once_with('edgechromium')

    def test_windows_session_ending_sets_exit_and_unsubscribes(self):
        handlers = []
        class Event:
            def __iadd__(self, handler):
                handlers.append(handler)
                return self
            def __isub__(self, handler):
                handlers.remove(handler)
                return self
        gui = Mock()
        gui.initialize.return_value.renderer = 'edgechromium'
        events = SimpleNamespace(SessionEnding=Event())
        exiting = threading.Event()
        with patch.dict(sys.modules, {'webview.guilib': gui,
                                     'Microsoft.Win32': SimpleNamespace(SystemEvents=events)}):
            unsubscribe = app.require_webview2(exiting.set)
            self.assertFalse(exiting.is_set())
            handlers[0](None, None)
            self.assertTrue(exiting.is_set())
            unsubscribe()
            self.assertEqual(handlers, [])

    def test_directory_acl_prepared_before_private_files(self):
        target = self.directory / 'profile'
        app.prepare_directory(target)
        self.assertTrue((target / 'data').is_dir())
        self.assertTrue((target / 'logs').is_dir())


if __name__ == '__main__':
    unittest.main()
