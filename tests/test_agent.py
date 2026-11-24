"""Run from the project root: python -m unittest discover -s tests -v."""

import builtins
import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

# Keep unittest discovery independent of whether tests is a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent

TOKEN = "test-token-" + "a" * 32


class PsutilError(Exception):
    pass


def fake_psutil():
    return types.SimpleNamespace(
        Error=PsutilError,
        cpu_percent=Mock(return_value=37.5),
        virtual_memory=Mock(return_value=types.SimpleNamespace(percent=50, total=1000, used=500)),
        disk_partitions=Mock(return_value=[types.SimpleNamespace(mountpoint="/")]),
        disk_usage=Mock(return_value=types.SimpleNamespace(total=2000, used=1000, percent=50)),
        boot_time=Mock(return_value=900),
        win_service_get=Mock(return_value=Mock(status=Mock(return_value="running"))),
    )


class ConfigurationTests(unittest.TestCase):
    def test_token_validation(self):
        self.assertEqual(agent.validate_token(TOKEN), TOKEN)
        for value in (None, "", "x" * 23, TOKEN + "\n", TOKEN + "\r", TOKEN + " ", TOKEN + "\x00", TOKEN + "é"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                agent.validate_token(value)

    def test_services_validation(self):
        self.assertEqual(agent.validate_services(["ssh", "ssh", "cron.service"]), ("ssh", "cron.service"))
        self.assertEqual(agent.validate_services(["MSSQL$SQLEXPRESS", "Windows Service"]), ("MSSQL$SQLEXPRESS", "Windows Service"))
        for names in (["--help"], [""], ["a\nb"], ["x" * 129], [str(n) for n in range(33)]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                agent.validate_services(names)

    def test_import_without_psutil_and_refuse_start(self):
        original_import = builtins.__import__

        def no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("not installed")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("agent_without_psutil", agent.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", side_effect=no_psutil):
            spec.loader.exec_module(module)
        self.assertIsNone(module.psutil)
        with self.assertRaisesRegex(RuntimeError, "requirements-agent"):
            module.AgentServer(("127.0.0.1", 0), TOKEN)

    def test_cli_defaults_and_repeated_services(self):
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.dict(agent.os.environ, {"NETMON_AGENT_TOKEN": TOKEN}), patch.object(agent, "AgentServer", return_value=server) as factory, contextlib.redirect_stderr(io.StringIO()) as output:
            agent.main(["--services", "ssh", "cron", "--services", "spooler"])
        factory.assert_called_once_with(("127.0.0.1", 9105), TOKEN, ("ssh", "cron", "spooler"), [])
        server.server_close.assert_called_once()
        self.assertIn("plaintext", output.getvalue())
        self.assertNotIn(TOKEN, output.getvalue())

    def test_cli_missing_token(self):
        with patch.dict(agent.os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            agent.main([])
        self.assertEqual(raised.exception.code, 2)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.ps = fake_psutil()
        self.patcher = patch.object(agent, "psutil", self.ps)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_schema_and_cpu_sampling(self):
        with patch.object(agent.time, "time", return_value=1000), patch.object(agent.socket, "gethostname", return_value="host"), patch.object(agent.platform, "system", return_value="Linux"), patch.object(agent, "service_status", return_value="active"):
            result = agent.collect_metrics(("ssh",))
        self.assertEqual(result, {
            "hostname": "host", "os": "Linux", "cpu_percent": 37.5,
            "memory": {"percent": 50, "total": 1000, "used": 500},
            "disks": [{"mountpoint": "/", "total": 2000, "used": 1000, "percent": 50}],
            "uptime_seconds": 100, "services": [{"name": "ssh", "status": "active"}],
        })
        self.ps.cpu_percent.assert_called_once_with(interval=0.2)

    def test_inaccessible_disks_and_negative_uptime(self):
        self.ps.disk_usage.side_effect = PermissionError()
        with patch.object(agent.time, "time", return_value=800):
            result = agent.collect_metrics()
        self.assertEqual(result["disks"], [])
        self.assertEqual(result["uptime_seconds"], 0)

    def test_surrogate_mountpoint_and_unicode_labels(self):
        import server
        original = '/mnt/磁碟\udcff'
        self.ps.disk_partitions.return_value = [types.SimpleNamespace(mountpoint=original)]
        with patch.object(agent.socket, 'gethostname', return_value='主機😀'), patch.object(agent.platform, 'system', return_value='Linux'):
            metrics = agent.collect_metrics()
        self.ps.disk_usage.assert_called_once_with(original)
        self.assertEqual(metrics['hostname'], '主機😀')
        self.assertEqual(metrics['disks'][0]['mountpoint'], '/mnt/磁碟?')
        self.assertEqual(server.validate_metrics(json.loads(json.dumps(metrics))), metrics)
        self.assertLessEqual(len(agent.display_text('\udcff' * 600, 512)), 512)

    def test_disk_limit(self):
        self.ps.disk_partitions.return_value = [types.SimpleNamespace(mountpoint=str(n)) for n in range(200)]
        self.assertEqual(len(agent.collect_metrics()["disks"]), agent.MAX_DISKS)

    def test_windows_service_and_errors(self):
        with patch.object(agent.platform, "system", return_value="Windows"):
            self.assertEqual(agent.service_status("Spooler"), "running")
            self.ps.win_service_get.assert_called_once_with("Spooler")
            self.ps.win_service_get.side_effect = PsutilError()
            self.assertEqual(agent.service_status("missing"), "unknown")

    def test_linux_service_and_errors(self):
        with patch.object(agent.platform, "system", return_value="Linux"), patch.object(agent.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(stdout="active\n", returncode=0)
            self.assertEqual(agent.service_status("ssh"), "active")
            run.assert_called_once_with(["systemctl", "is-active", "--", "ssh"], capture_output=True, text=True, timeout=2, check=False)
            for error in (FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 2), OSError()):
                run.side_effect = error
                self.assertEqual(agent.service_status("ssh"), "unknown")
            run.side_effect = None
            run.return_value = types.SimpleNamespace(stdout="unexpected diagnostic\n", returncode=1)
            self.assertEqual(agent.service_status("ssh"), "unknown")

    def test_linux_inactive_vs_missing_service(self):
        with patch.object(agent.platform, "system", return_value="Linux"), patch.object(agent.subprocess, "run") as run:
            for load_state, expected in (("loaded", "inactive"), ("not-found", "unknown")):
                run.side_effect = [
                    types.SimpleNamespace(stdout="inactive\n", returncode=3),
                    types.SimpleNamespace(stdout=load_state, returncode=0),
                ]
                self.assertEqual(agent.service_status("missing"), expected)
            self.assertEqual(run.call_args.args[0], ["systemctl", "show", "--property=LoadState", "--value", "--", "missing"])

    def test_unsupported_services(self):
        with patch.object(agent.platform, "system", return_value="Darwin"):
            self.assertEqual(agent.service_status("ssh"), "unknown")


class CollectorTests(unittest.TestCase):
    def test_stalled_collection_is_single_flight_and_recovers(self):
        collector = agent.MetricsCollector(('ssh',))
        entered = threading.Event()
        release = threading.Event()
        def stalled(services):
            entered.set()
            release.wait(3)
            return {'fresh': True}
        try:
            with patch.object(agent, 'collect_metrics', side_effect=stalled) as collect, patch.object(agent, 'COLLECTION_WAIT', 0.02):
                for _ in range(10):
                    started = time.monotonic()
                    with self.assertRaises(RuntimeError):
                        collector.get()
                    self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(entered.is_set())
                collect.assert_called_once_with(('ssh',))
                release.set()
                with collector.condition:
                    self.assertTrue(collector.condition.wait_for(lambda: not collector.running, 2))
                self.assertEqual(collector.get(), {'fresh': True})
        finally:
            release.set()
            collector.close()

    def test_stale_snapshot_and_late_collection_are_rejected(self):
        collector = agent.MetricsCollector(())
        collector.snapshot = {'stale': True}
        collector.started = time.monotonic() - agent.MAX_SNAPSHOT_AGE - 1
        entered = threading.Event()
        release = threading.Event()
        def stalled(services):
            entered.set()
            release.wait(3)
            return {'late': True}
        try:
            with patch.object(agent, 'collect_metrics', side_effect=stalled), patch.object(agent, 'COLLECTION_WAIT', 0.02):
                with self.assertRaises(RuntimeError):
                    collector.get()
                self.assertTrue(entered.is_set())
                # A late result must retain the original collection start time.
                with patch.object(agent, 'MAX_SNAPSHOT_AGE', 0):
                    release.set()
                    with collector.condition:
                        self.assertTrue(collector.condition.wait_for(lambda: not collector.running, 2))
                    with self.assertRaises(RuntimeError):
                        collector.get()
        finally:
            release.set()
            collector.close()

    def test_periodic_refresh_survives_slow_collection_and_sparse_polls(self):
        collector = agent.MetricsCollector(())
        def slow(services):
            time.sleep(0.03)  # Successful, but longer than the HTTP wait budget.
            return {'fresh': True}
        try:
            with patch.object(agent, 'collect_metrics', side_effect=slow) as collect, \
                    patch.object(agent, 'COLLECTION_WAIT', 0.005), \
                    patch.object(agent, 'COLLECTION_INTERVAL', 0.02), \
                    patch.object(agent, 'MAX_SNAPSHOT_AGE', 0.12):
                with self.assertRaises(RuntimeError):
                    collector.get()
                for _ in range(3):
                    time.sleep(0.16)  # Poll less frequently than snapshot expiry.
                    self.assertEqual(collector.get(), {'fresh': True})
                self.assertGreater(collect.call_count, 3)
                collector.close()
                collector.worker.join(timeout=1)
                self.assertFalse(collector.worker.is_alive())
        finally:
            collector.close()

    def test_close_is_bounded_and_unblocks_waiters(self):
        collector = agent.MetricsCollector(())
        entered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        failures = []
        waiter_count = 0
        original_wait = collector.condition.wait_for
        def observed_wait(predicate, timeout=None):
            nonlocal waiter_count
            waiter_count += 1  # All callers hold the condition lock here.
            if waiter_count == agent.MAX_CLIENTS:
                waiting.set()
            return original_wait(predicate, timeout)
        def stalled(services):
            entered.set()
            release.wait(5)
            return {'late': True}
        def request():
            try:
                collector.get()
            except RuntimeError:
                failures.append(True)
        callers = [threading.Thread(target=request) for _ in range(agent.MAX_CLIENTS)]
        try:
            with patch.object(agent, 'collect_metrics', side_effect=stalled), \
                    patch.object(agent, 'COLLECTION_WAIT', 3), \
                    patch.object(collector.condition, 'wait_for', side_effect=observed_wait):
                for caller in callers:
                    caller.start()
                self.assertTrue(entered.wait(2))
                self.assertTrue(waiting.wait(2))
                started = time.monotonic()
                collector.close()
                for caller in callers:
                    caller.join(timeout=0.5)
                    self.assertFalse(caller.is_alive())
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(len(failures), agent.MAX_CLIENTS)
                release.set()
                collector.worker.join(timeout=1)
                self.assertFalse(collector.worker.is_alive())
                self.assertIsNone(collector.snapshot)
        finally:
            collector.close()
            release.set()
            for caller in callers:
                if caller.ident is not None:
                    caller.join(timeout=1)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher = patch.object(agent, "psutil", fake_psutil())
        cls.patcher.start()
        cls.server = agent.AgentServer(("127.0.0.1", 0), TOKEN)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.patcher.stop()

    def setUp(self):
        self.server.collector.close()
        self.server.collector = agent.MetricsCollector(self.server.services)

    def request(self, path="/metrics", token=TOKEN, method="GET"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            headers = {} if token is None else {"Authorization": "Bearer " + token}
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_authentication(self):
        with patch.object(agent, "collect_metrics") as collect:
            for token in (None, "incorrect", TOKEN + "x"):
                status, headers, body = self.request(token=token)
                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                self.assertNotIn(TOKEN.encode(), body)
            collect.assert_not_called()

    def test_duplicate_authorization_rejected(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.putrequest("GET", "/metrics")
            for _ in range(2):
                connection.putheader("Authorization", "Bearer " + TOKEN)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()

    def test_metrics_response(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(json.loads(body)["cpu_percent"], 37.5)

    def test_stalled_metrics_do_not_exhaust_http_slots(self):
        entered = threading.Event()
        release = threading.Event()
        def stalled(services):
            entered.set()
            release.wait(5)
            return {'done': True}
        try:
            with patch.object(agent, 'collect_metrics', side_effect=stalled) as collect, patch.object(agent, 'COLLECTION_WAIT', 0.02):
                for _ in range(agent.MAX_CLIENTS + 2):
                    self.assertEqual(self.request()[0], 503)
                self.assertTrue(entered.is_set())
                collect.assert_called_once()
                self.assertEqual(self.request(token='wrong')[0], 401)
                release.set()
                with self.server.collector.condition:
                    self.assertTrue(self.server.collector.condition.wait_for(lambda: not self.server.collector.running, 2))
        finally:
            release.set()

    def test_only_exact_metrics_path(self):
        for path in ("/", "/metrics?x=1", "/metrics/"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request(method="POST")[0], 501)

    def test_errors_do_not_leak_details(self):
        with patch.object(agent, "collect_metrics", side_effect=RuntimeError(TOKEN)), contextlib.redirect_stderr(io.StringIO()) as output:
            status, _, body = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(TOKEN, output.getvalue())

    def test_response_size_limit(self):
        with patch.object(agent, "collect_metrics", return_value={"large": "x" * agent.MAX_RESPONSE_BYTES}):
            status, _, body = self.request()
        self.assertEqual(status, 503)
        self.assertLess(len(body), agent.MAX_RESPONSE_BYTES)

    def test_absolute_header_deadline(self):
        handler = object.__new__(agent.MetricsHandler)
        handler.connection = Mock()
        handler.expire_headers()
        handler.connection.shutdown.assert_called_once_with(agent.socket.SHUT_RDWR)
        handler.connection.shutdown.side_effect = OSError()
        handler.expire_headers()  # Already-closed sockets are harmless.
        handler.header_deadline = Mock()
        with patch.object(agent.BaseHTTPRequestHandler, "parse_request", return_value=True):
            self.assertTrue(handler.parse_request())
        handler.header_deadline.cancel.assert_called_once()

    def test_source_ip_allowlist(self):
        server = agent.AgentServer(("127.0.0.1", 0), TOKEN, allowed_ips=["192.168.6.6"])
        try:
            request = Mock()
            server.process_request(request, ("127.0.0.1", 1))
            request.close.assert_called_once()
            self.assertEqual(server.allowed_ips, frozenset({"192.168.6.6"}))
        finally:
            server.server_close()

    def test_concurrent_connection_limit(self):
        # Isolate admission control from live handlers still finishing cleanup.
        server = agent.AgentServer(("127.0.0.1", 0), TOKEN)
        try:
            for _ in range(agent.MAX_CLIENTS):
                self.assertTrue(server.slots.acquire(blocking=False))
            request = Mock()
            server.process_request(request, ("127.0.0.1", 1))
            request.close.assert_called_once()
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
