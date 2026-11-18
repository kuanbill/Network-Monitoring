"""Optional real-agent loopback integration; skipped if psutil is not installed."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import server


@unittest.skipUnless(importlib.util.find_spec('psutil'), 'Install requirements-agent.txt for real-agent integration')
class RealAgentTests(unittest.TestCase):
    def test_authenticated_agent_and_monitor(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            number = listener.getsockname()[1]
        token = secrets.token_urlsafe(32)
        env = {**os.environ, 'NETMON_AGENT_TOKEN': token}
        process = subprocess.Popen([sys.executable, str(Path(server.__file__).parent / 'agent.py'), '--port', str(number)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f'http://127.0.0.1:{number}/metrics'
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urlopen(Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=3) as response:
                        metrics = json.load(response)
                    break
                except (URLError, OSError):
                    if time.monotonic() > deadline:
                        self.fail('Real agent did not start')
                    time.sleep(0.1)
            self.assertIn('cpu_percent', metrics)
            self.assertGreater(metrics['memory']['total'], 0)
            self.assertIsInstance(metrics['disks'], list)
            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(url, timeout=3)
            self.assertEqual(unauthorized.exception.code, 401)
            unauthorized.exception.close()
            host = server.validate_host({'ip': '127.0.0.1', 'agent_enabled': True, 'agent_port': number, 'agent_token': token})
            result = server.inspect_host(host)
            self.assertEqual(result['status'], 'up')
            self.assertTrue(result['agent']['ok'])
            self.assertEqual(result['agent']['metrics']['hostname'], metrics['hostname'])
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
