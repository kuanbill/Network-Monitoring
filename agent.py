"""Optional LAN monitoring agent (Python 3.9+, Windows/Linux/macOS).

Install: python -m pip install -r requirements-agent.txt
Set NETMON_AGENT_TOKEN to a random, printable ASCII token of at least 24
characters (no spaces/newlines). It is required; never pass it on the CLI.
POSIX example: export NETMON_AGENT_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
PowerShell: $env:NETMON_AGENT_TOKEN = '<random-token-at-least-24-characters>'
Run: python agent.py --bind 127.0.0.1 --port 9105 --services ssh cron
Repeat --services to add names: --services ssh --services cron
Fetch GET /metrics with Authorization: Bearer <token>.

WARNING: HTTP is plaintext, including the bearer token. Use a trusted LAN
with firewall restrictions or a VPN; never expose this agent to the internet.
Binding to a LAN address (or 0.0.0.0) is an explicit opt-in to remote access.
Service status is 'unknown' when unsupported, unavailable or inaccessible.
"""

import argparse
import hmac
import ipaddress
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    psutil = None

MAX_SERVICES = 32
MAX_DISKS = 128
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CLIENTS = 8
COLLECTION_WAIT = 0.75
COLLECTION_INTERVAL = 5
MAX_SNAPSHOT_AGE = 15


def validate_token(token):
    if not token or len(token) < 24 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("NETMON_AGENT_TOKEN must contain at least 24 printable ASCII characters without whitespace")
    return token


def validate_services(names):
    names = tuple(dict.fromkeys(names))
    if len(names) > MAX_SERVICES or any(
        not n or len(n) > 128 or n.startswith("-") or not n.isprintable()
        for n in names
    ):
        raise ValueError("service names must be 1-128 printable characters, not start with '-'; at most 32 services")
    return names


def service_status(name):
    try:
        if platform.system() == "Windows":
            return str(psutil.win_service_get(name).status())[:128]
        if platform.system() == "Linux":
            result = subprocess.run(
                ["systemctl", "is-active", "--", name],
                capture_output=True, text=True, timeout=2, check=False,
            )
            status = result.stdout.strip()
            if status == "inactive":
                # is-active alone can call a nonexistent unit 'inactive'.
                loaded = subprocess.run(
                    ["systemctl", "show", "--property=LoadState", "--value", "--", name],
                    capture_output=True, text=True, timeout=2, check=False,
                )
                if loaded.returncode != 0 or loaded.stdout.strip() != "loaded":
                    return "unknown"
            if status in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance"}:
                return status
    except (OSError, subprocess.SubprocessError, AttributeError, psutil.Error if psutil else OSError):
        pass
    return "unknown"


def display_text(value, maximum):
    # POSIX filenames may contain surrogateescape code points; keep valid UTF-8 labels.
    return str(value).encode('utf-8', errors='replace').decode('utf-8')[:maximum]


def collect_metrics(services=()):
    if psutil is None:
        raise RuntimeError("psutil is required to collect metrics")
    cpu = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory()
    disks = []
    seen = set()
    for partition in psutil.disk_partitions(all=False)[:MAX_DISKS]:
        mountpoint = partition.mountpoint
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        try:
            usage = psutil.disk_usage(mountpoint)
        except (OSError, psutil.Error):
            continue
        disks.append({"mountpoint": display_text(mountpoint, 512), "total": usage.total,
                      "used": usage.used, "percent": usage.percent})
    return {
        "hostname": display_text(socket.gethostname(), 512),
        "os": display_text(platform.system(), 128),
        "cpu_percent": cpu,
        "memory": {"percent": memory.percent, "total": memory.total, "used": memory.used},
        "disks": disks,
        "uptime_seconds": max(0, time.time() - psutil.boot_time()),
        "services": [{"name": name, "status": service_status(name)} for name in services],
    }


class MetricsCollector:
    """Single-flight daemon collection; a stalled OS query never owns an HTTP slot.

    No replacement workers are spawned while a collection is stuck. Cached data
    expires based on the start of collection, not when a slow query finally ends.
    """

    def __init__(self, services):
        self.services = services
        self.condition = threading.Condition()
        self.running = False
        self.closed = False
        self.snapshot = None
        self.started = 0
        self.worker = None

    def _collect(self):
        while True:
            started = time.monotonic()
            try:
                snapshot = collect_metrics(self.services)
            except Exception:
                snapshot = None
            with self.condition:
                self.running = False
                if not self.closed:
                    self.snapshot = snapshot
                    self.started = started
                self.condition.notify_all()
                if self.condition.wait_for(lambda: self.closed, COLLECTION_INTERVAL):
                    return
                self.running = True

    def get(self):
        with self.condition:
            if self.closed:
                raise RuntimeError('metrics unavailable')
            if self.worker is None:
                self.running = True
                self.worker = threading.Thread(target=self._collect, daemon=True)
                try:
                    self.worker.start()
                except Exception:
                    self.running = False
                    self.worker = None
                    raise
            if self.snapshot is None or time.monotonic() - self.started > MAX_SNAPSHOT_AGE:
                self.condition.wait_for(lambda: not self.running or self.closed, COLLECTION_WAIT)
            if self.closed or self.snapshot is None or time.monotonic() - self.started > MAX_SNAPSHOT_AGE:
                raise RuntimeError('metrics unavailable')
            return self.snapshot

    def close(self):
        with self.condition:
            self.closed = True
            self.snapshot = None
            self.condition.notify_all()


class AgentServer(ThreadingHTTPServer):
    """Bound concurrent connections (including slow clients) before spawning threads."""

    daemon_threads = True
    request_queue_size = MAX_CLIENTS

    def __init__(self, address, token, services=(), allowed_ips=()):
        self.allowed_ips = frozenset(str(ipaddress.ip_address(ip)) for ip in allowed_ips)
        self.token = validate_token(token).encode("ascii")
        self.services = validate_services(services)
        if psutil is None:
            raise RuntimeError("Install optional agent dependencies: pip install -r requirements-agent.txt")
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.collector = MetricsCollector(self.services)
        super().__init__(address, MetricsHandler)

    def server_close(self):
        self.collector.close()
        super().server_close()

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request, client_address):
        if self.allowed_ips and client_address[0] not in self.allowed_ips:
            self.shutdown_request(request)
            return
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Never emit request headers, tokens or exception details.
        pass


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "NetmonAgent"
    sys_version = ""

    def setup(self):
        super().setup()
        # A per-read socket timeout alone does not stop trickled request headers.
        self.header_deadline = threading.Timer(5, self.expire_headers)
        self.header_deadline.daemon = True
        self.header_deadline.start()

    def expire_headers(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            self.header_deadline.cancel()

    def finish(self):
        self.header_deadline.cancel()
        super().finish()

    def log_message(self, format, *args):
        # Default request logging can include attacker-supplied secrets.
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            status, body = 503, b'{"error":"metrics unavailable"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        headers = self.headers.get_all("Authorization", [])
        supplied = headers[0].encode("utf-8") if len(headers) == 1 else b""
        if not hmac.compare_digest(supplied, b"Bearer " + self.server.token):
            self.send_json(401, {"error": "unauthorized"})
            return
        if self.path != "/metrics":
            self.send_json(404, {"error": "not found"})
            return
        try:
            metrics = self.server.collector.get()
            self.send_json(200, metrics)
        except Exception:
            self.send_json(503, {"error": "metrics unavailable"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9105)
    parser.add_argument("--allow-ip", action="append", default=[], help="Allow only this source IP; repeat for multiple sources")
    parser.add_argument("--services", nargs="+", action="append", default=[], metavar="NAME")
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        token = validate_token(os.environ.get("NETMON_AGENT_TOKEN"))
        services = validate_services([name for group in args.services for name in group])
        server = AgentServer((args.bind, args.port), token, services, args.allow_ip)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))
    print("WARNING: HTTP is plaintext. Restrict access with a firewall/VPN; never expose to the internet.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
