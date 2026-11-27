"""LAN service monitor. Python 3.10+, standard library only."""
import argparse
import concurrent.futures
import copy
import http.client
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import threading
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit
from notifications import DOCKER_UNIT, NotificationManager
from website_probe import WEBSITE_DOMAINS, probe_website

ROOT = Path(__file__).resolve().parent
PRIVATE = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8')]
TIMEOUT = 1.5
MAX_CLIENTS = 32
REQUEST_DEADLINE = 10
DOCKER_DETAILS = {
    'active': 'systemd 回報 Docker 服務運行中',
    'inactive': 'Docker 服務未運行',
    'failed': 'Docker 服務回報失敗',
    'activating': 'Docker 服務正在啟動，尚未確認正常',
    'deactivating': 'Docker 服務正在停止',
    'reloading': 'Docker 服務正在重新載入',
    'maintenance': 'Docker 服務處於維護狀態',
    'unknown': '無法確認 Docker 服務狀態，請確認 Agent 已設定檢查 docker.service',
    'agent_unavailable': 'Agent 無法提供資料，不能據此判定 Docker 服務狀態',
}
WEBSITE_DETAILS = {
    'dns': 'DNS 查詢失敗或沒有可用的公開位址',
    'certificate': 'HTTPS 憑證驗證失敗',
    'connection': '無法取得 HTTPS 回應',
    'timeout': '網站檢查逾時',
    'busy': '無資料：前次網站檢查未結束',
}


def valid_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise ValueError('請輸入有效的 IPv4 位址')
    if not any(ip in net for net in PRIVATE):
        raise ValueError('僅允許私有區網或本機 IPv4 位址')
    return str(ip)


def port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError('連接埠必須為 1–65535 的整數')
    return value


def scan_addresses(value):
    if not isinstance(value, str):
        raise ValueError('請指定 IPv4 CIDR 或起點-終點')
    try:
        if '-' in value:
            a, b = (ipaddress.IPv4Address(x.strip()) for x in value.split('-', 1))
            count = int(b) - int(a) + 1
            if not 1 <= count <= 1024:
                raise ValueError()
            addresses = [str(ipaddress.IPv4Address(int(a) + i)) for i in range(count)]
        else:
            net = ipaddress.IPv4Network(value.strip(), strict=False)
            if net.num_addresses > 1024:
                raise ValueError()
            addresses = [str(x) for x in net.hosts()]
        return [valid_ip(x) for x in addresses]
    except (ValueError, TypeError):
        raise ValueError('掃描範圍須為私有 IPv4，最多 1024 個位址（例如 192.168.1.0/24）')


def validate_host(data, previous=None):
    if not isinstance(data, dict):
        raise ValueError('主機設定須為 JSON 物件')
    ip = valid_ip(data.get('ip'))
    name = data.get('name', ip)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('主機名稱長度須為 1–100')
    checks = data.get('checks', [])
    if not isinstance(checks, list) or len(checks) > 16:
        raise ValueError('每台主機最多 16 個服務檢查')
    clean = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError('服務檢查格式錯誤')
        if check.get('type') == 'tcp':
            clean.append({'type': 'tcp', 'port': port(check.get('port'))})
        elif check.get('type') == 'http':
            url = check.get('url')
            if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
                raise ValueError('HTTP URL 格式錯誤')
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or parts.hostname != ip or parts.username or parts.password or parts.fragment:
                raise ValueError('HTTP URL 必須使用此主機的 IP，不可包含帳密或片段')
            if parts.port is not None:
                port(parts.port)
            clean.append({'type': 'http', 'url': url})
        else:
            raise ValueError('只支援 TCP 或 HTTP 檢查')
    token = data.get('agent_token', (previous or {}).get('agent_token', ''))
    if not isinstance(token, str) or len(token) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('代理程式 token 須為可列印的 ASCII 字元（不可含空白）')
    enabled = data.get('agent_enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('agent_enabled 須為布林值')
    if enabled and len(token) < 24:
        raise ValueError('啟用代理程式須提供至少 24 字元的 token')
    flags = {}
    for key in ('docker_monitor_enabled', 'website_monitor_enabled'):
        value = data.get(key, (previous or {}).get(key, False))
        if not isinstance(value, bool):
            raise ValueError(f'{key} 須為布林值')
        if value and ip not in WEBSITE_DOMAINS:
            raise ValueError('此主機不支援固定的 Docker 或網站監控')
        flags[key] = value
    if flags['docker_monitor_enabled'] and not enabled:
        raise ValueError('Docker 監控需要 Agent；停用 Agent 前請先停用 Docker 監控')
    mac = data.get('mac_address', (previous or {}).get('mac_address', ''))
    if not isinstance(mac, str) or (mac and not re.fullmatch(r'[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}', mac)):
        raise ValueError('MAC 位址格式錯誤')
    mac = mac.replace('-', ':').upper()
    if mac and (int(mac[:2], 16) & 1 or mac == '00:00:00:00:00:00'):
        raise ValueError('請輸入主機網卡的單播 MAC 位址')
    # Reject strings that cannot be persisted as UTF-8 (e.g. lone surrogates).
    name.encode('utf-8')
    for check in clean:
        if check['type'] == 'http':
            check['url'].encode('utf-8')
    return {'id': (previous or {}).get('id', uuid.uuid4().hex), 'name': name.strip(), 'ip': ip,
            'checks': clean, 'agent_enabled': enabled, 'agent_port': port(data.get('agent_port', 9105)), 'agent_token': token,
            'mac_address': mac, **flags}


def ping(ip):
    args = ['ping', '-n', '1', '-w', '1000', ip] if os.name == 'nt' else ['ping', '-n', '-c', '1', '-W', '1', ip]
    start = time.monotonic()
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # Windows may return 0 for an ICMP destination-unreachable reply.
        ok = result.returncode == 0 and (os.name != 'nt' or b'ttl=' in result.stdout.lower())
        return ok, round((time.monotonic() - start) * 1000, 1) if ok else None
    except (OSError, subprocess.TimeoutExpired):
        return False, None


def tcp(ip, number):
    with socket.create_connection((ip, number), timeout=TIMEOUT):
        pass


def http_get(ip, number, path, secure=False, token=None):
    cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    connection = cls(ip, number, timeout=TIMEOUT)
    timer = None
    try:
        connection.connect()
        transport = connection.sock
        def expire():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()
        # Socket inactivity timeouts alone do not stop trickling responses.
        timer = threading.Timer(5, expire)
        timer.daemon = True
        timer.start()
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        connection.request('GET', quote(path, safe="/%?=&:+,;@!$'()*-._~"), headers=headers)
        response = connection.getresponse()
        body = response.read(262145) if token is not None else b''
        if len(body) > 262144:
            raise ValueError('代理程式回應過大')
        return response.status, body
    finally:
        if timer:
            timer.cancel()
        connection.close()


def validate_metrics(metrics):
    if not isinstance(metrics, dict):
        raise ValueError('代理程式資料格式錯誤')
    json.dumps(metrics, allow_nan=False, ensure_ascii=False).encode('utf-8')
    for key in ('hostname', 'os'):
        if not isinstance(metrics.get(key), str) or not metrics[key]:
            raise ValueError('代理程式文字欄位格式錯誤')

    def numbers(obj, keys):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError('代理程式數值欄位格式錯誤')
            if key in ('percent', 'cpu_percent') and value > 100:
                raise ValueError('代理程式百分比須為 0–100')

    numbers(metrics, ('cpu_percent', 'uptime_seconds'))
    if not isinstance(metrics.get('memory'), dict):
        raise ValueError('代理程式記憶體欄位格式錯誤')
    numbers(metrics['memory'], ('percent', 'total', 'used'))
    for key, maximum, text_fields in [('disks', 128, ('mountpoint',)), ('services', 32, ('name', 'status'))]:
        items = metrics.get(key)
        if not isinstance(items, list) or len(items) > maximum:
            raise ValueError('代理程式清單格式錯誤')
        for item in items:
            if not isinstance(item, dict) or any(not isinstance(item.get(field), str) or not item[field] for field in text_fields):
                raise ValueError('代理程式清單項目格式錯誤')
            if key == 'disks':
                numbers(item, ('percent', 'total', 'used'))
    return metrics


def inspect_host(host):
    alive, latency = ping(host['ip'])
    results = []
    for check in host['checks']:
        started = time.monotonic()
        label = 'TCP ' + str(check['port']) if check['type'] == 'tcp' else check['url']
        try:
            if check['type'] == 'tcp':
                tcp(host['ip'], check['port'])
                detail, ok = '可連線', True
            else:
                parts = urlsplit(check['url'])
                status, _ = http_get(host['ip'], parts.port or (443 if parts.scheme == 'https' else 80), (parts.path or '/') + ('?' + parts.query if parts.query else ''), parts.scheme == 'https')
                detail, ok = 'HTTP ' + str(status), 200 <= status < 400
            elapsed = round((time.monotonic() - started) * 1000, 1)
            results.append({'type': check['type'], 'label': label, 'ok': ok, 'detail': detail, 'latency_ms': elapsed})
            # Any HTTP response proves reachability, including error responses.
            alive = True
            latency = latency if latency is not None else elapsed
        except (OSError, ValueError, http.client.HTTPException) as exc:
            results.append({'type': check['type'], 'label': label, 'ok': False, 'detail': str(exc)[:250], 'latency_ms': None})
    agent = None
    if host['agent_enabled']:
        try:
            status, body = http_get(host['ip'], host['agent_port'], '/metrics', token=host['agent_token'])
            alive = True
            if status != 200:
                raise ValueError('代理程式 HTTP ' + str(status))
            def reject_constant(value):
                raise ValueError('代理程式包含非有限數值')
            # Also rejects exponent overflow, lone surrogates and malformed lists.
            metrics = validate_metrics(json.loads(body, parse_constant=reject_constant))
            agent = {'ok': True, 'metrics': metrics}
            alive = True
        except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
            agent = {'ok': False, 'error': str(exc)[:250]}
    services = agent.get('metrics', {}).get('services', []) if agent else []
    checks = {}
    if host.get('docker_monitor_enabled'):
        units = [service for service in services if service['name'] == DOCKER_UNIT]
        if agent and agent['ok']:
            code = units[0]['status'] if len(units) == 1 else 'unknown'
            if code == 'agent_unavailable' or code not in DOCKER_DETAILS:
                code = 'unknown'
            ok = code == 'active'
        else:
            code, ok = 'agent_unavailable', None
        checks['docker'] = {'ok': ok, 'code': code, 'label': 'Docker 服務',
                            'detail': DOCKER_DETAILS[code], 'latency_ms': None}
        services = [service for service in services if service['name'] != DOCKER_UNIT]
    services_failed = any(service['status'] not in ('running', 'active') for service in services)
    host_failed = any(not c['ok'] for c in results) or (agent is not None and not agent['ok']) or services_failed
    host_status = ('degraded' if host_failed else 'up') if alive else 'down'
    if host.get('website_monitor_enabled'):
        website = probe_website(host['ip'])
        detail = f"HTTP {website['http_status']}" if website['code'] == 'http' else WEBSITE_DETAILS[website['code']]
        checks['website'] = {**website, 'label': f"網站 {WEBSITE_DOMAINS[host['ip']]}", 'detail': detail}
    status = 'degraded' if alive and any(check['ok'] is False for check in checks.values()) else host_status
    return {'timestamp': time.time(), 'status': status, 'host_status': host_status,
            'latency_ms': latency, 'reachable': alive, 'checks': results, 'agent': agent, **checks}


def validate_settings(data, previous=None):
    if not isinstance(data, dict) or set(data) - {'refresh_interval', 'monitor_interval'}:
        raise ValueError('更新頻率設定格式不正確')
    result = dict(previous or {'refresh_interval': 5, 'monitor_interval': 30})
    result.update(data)
    for key, minimum in (('refresh_interval', 1), ('monitor_interval', 5)):
        value = result[key]
        if type(value) is not int or not minimum <= value <= 3600:
            label = '畫面同步' if key == 'refresh_interval' else '背景監測'
            raise ValueError(f'{label}秒數須為 {minimum}–3600 的整數')
    return result


class Monitor:
    def __init__(self, data_dir, interval=30):
        self.path = Path(data_dir) / 'hosts.json'
        self.settings_path = Path(data_dir) / 'settings.json'
        defaults = validate_settings({'monitor_interval': interval})
        self.settings = validate_settings(json.loads(self.settings_path.read_text(encoding='utf-8')), defaults) if self.settings_path.exists() else defaults
        self.notifications = NotificationManager(data_dir)
        self.lock = threading.RLock()
        self.cycle_lock = threading.Lock()
        self.interval = self.settings['monitor_interval']
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.hosts = []
        self.last_wol = float('-inf')

        self.states = {}
        self.scan = {'running': False, 'progress': 0, 'total': 0, 'results': [], 'error': None}
        if self.path.exists():
            self.hosts = [validate_host(h, h) for h in json.loads(self.path.read_text(encoding='utf-8'))]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates a unique file with mode 0600 before any secrets are written.
        descriptor, name = tempfile.mkstemp(prefix='hosts-', suffix='.tmp', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(self.hosts, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def export_config(self):
        with self.lock:
            notification_settings = self.notifications.public()
            return {
                'format': 'netword-monitor', 'version': 1,
                'settings': dict(self.settings),
                'hosts': [{k: copy.deepcopy(v) for k, v in host.items() if k != 'agent_token'}
                          for host in self.hosts],
                'notifications': {k: v for k, v in notification_settings.items()
                                  if k not in ('password_set', 'delivery')},
            }

    def get_settings(self):
        with self.lock:
            return dict(self.settings)

    def update_settings(self, data):
        with self.lock:
            settings = validate_settings(data, self.settings)
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='settings-', suffix='.tmp', dir=self.settings_path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(settings, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.settings_path)
            finally:
                temporary.unlink(missing_ok=True)
            changed = self.interval != settings['monitor_interval']
            self.settings = settings
            self.interval = settings['monitor_interval']
            if changed:
                self.wake.set()
            return dict(settings)

    def wake_host(self, identity):
        with self.lock:
            host = next((h for h in self.hosts if h['id'] == identity), None)
            if host is None:
                raise KeyError('找不到主機')
            if not host['mac_address'] or host['ip'].startswith('127.'):
                raise ValueError('只可喚醒已設定 MAC 位址的區網主機')
            now = time.monotonic()
            if now - self.last_wol < 3:
                raise ValueError('請間隔至少 3 秒再送出喚醒封包')
            self.last_wol = now
            mac = host['mac_address']
        packet = b'\xff' * 6 + bytes.fromhex(mac.replace(':', '')) * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, ('255.255.255.255', 9))

    def state(self):
        with self.lock:
            hosts = []
            for host in self.hosts:
                public = {k: v for k, v in host.items() if k != 'agent_token'}
                public['agent_token_set'] = bool(host['agent_token'])
                public.update(self.states.get(host['id'], {'last': None, 'history': []}))
                hosts.append(public)
            return copy.deepcopy({'hosts': hosts, 'interval': self.interval, 'settings': self.settings})

    def change(self, data, identity=None, delete=False):
        with self.lock:
            old_hosts, old_states = list(self.hosts), dict(self.states)
            previous = next((h for h in self.hosts if h['id'] == identity), None)
            if identity and previous is None:
                raise KeyError('找不到主機')
            if delete:
                self.hosts.remove(previous)
                self.states.pop(identity, None)
                result = {'ok': True}
            else:
                if not previous and len(self.hosts) >= 256:
                    raise ValueError('最多可監控 256 台主機')
                host = validate_host(data, previous)
                if any(h['ip'] == host['ip'] and h['id'] != host['id'] for h in self.hosts):
                    raise ValueError('此 IP 已加入監控')
                if previous:
                    self.hosts[self.hosts.index(previous)] = host
                    self.states.pop(identity, None)
                else:
                    self.hosts.append(host)
                result = {k: v for k, v in host.items() if k != 'agent_token'}
                result['agent_token_set'] = bool(host['agent_token'])
            try:
                self.save()
            except (OSError, ValueError):
                self.hosts, self.states = old_hosts, old_states
                raise
            if delete or (previous and previous['ip'] != host['ip']):
                self.notifications.forget(identity)
            elif previous:
                affected = set()
                if previous['checks'] != host['checks']:
                    affected.add('host')
                if any(previous[key] != host[key] for key in
                       ('agent_enabled', 'agent_port', 'agent_token', 'docker_monitor_enabled')):
                    affected.update(('host', 'docker'))
                if previous['website_monitor_enabled'] != host['website_monitor_enabled']:
                    affected.add('website')
                if affected:
                    self.notifications.forget(identity, affected)
        self.wake.set()
        return result

    def cycle(self):
        with self.cycle_lock:
            with self.lock:
                # Committed edits replace the host object. Its identity is the
                # revision marker even if a later edit restores identical values.
                hosts = [(host, copy.deepcopy(host)) for host in self.hosts]
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                futures = {pool.submit(inspect_host, snapshot): (original, snapshot)
                           for original, snapshot in hosts}
                for future in concurrent.futures.as_completed(futures):
                    original, host = futures[future]
                    try:
                        last = future.result()
                    except Exception as exc:
                        print('Monitor error:', type(exc).__name__, flush=True)
                        continue
                    with self.lock:
                        if not any(current is original for current in self.hosts):
                            continue
                        state = self.states.setdefault(host['id'], {'history': []})
                        state['last'] = last
                        state['history'].append({k: last[k] for k in ('timestamp', 'status', 'latency_ms')})
                        state['history'] = state['history'][-120:]
                        self.notifications.observe(host, last)

    def loop(self):
        while not self.stop.is_set():
            self.wake.clear()
            self.cycle()
            self.wake.wait(self.interval)

    def start_scan(self, data):
        addresses = scan_addresses(data.get('range'))
        ports = data.get('ports', [22, 80, 443, 3389])
        if not isinstance(ports, list) or len(ports) > 16:
            raise ValueError('掃描連接埠最多 16 個')
        ports = [port(p) for p in ports]
        with self.lock:
            if self.scan['running']:
                raise ValueError('掃描正在執行中')
            self.scan = {'running': True, 'progress': 0, 'total': len(addresses), 'results': [], 'error': None}
        threading.Thread(target=self.scan_run, args=(addresses, ports), daemon=True).start()
        return {'running': True}

    def scan_run(self, addresses, ports):
        def probe(ip):
            alive, latency = ping(ip)
            opened = []
            for p in ports:
                start = time.monotonic()
                try:
                    tcp(ip, p)
                    opened.append(p)
                    alive = True
                    if latency is None:
                        latency = round((time.monotonic() - start) * 1000, 1)
                except OSError:
                    pass
            return {'ip': ip, 'hostname': '', 'latency_ms': latency, 'open_ports': opened} if alive else None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
                for future in concurrent.futures.as_completed([pool.submit(probe, ip) for ip in addresses]):
                    result = future.result()
                    with self.lock:
                        self.scan['progress'] += 1
                        if result:
                            self.scan['results'].append(result)
                            self.scan['results'].sort(key=lambda h: int(ipaddress.IPv4Address(h['ip'])))
        except Exception as exc:
            with self.lock:
                self.scan['error'] = str(exc)[:250]
        finally:
            with self.lock:
                self.scan['running'] = False


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        # Cover request line, headers and body, including clients that trickle bytes.
        self.request_deadline = threading.Timer(self.server.request_deadline, self.expire_request)
        self.request_deadline.daemon = True
        self.request_deadline.start()

    def expire_request(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.request_deadline.cancel()
        super().finish()

    def log_message(self, fmt, *args):
        # Never log request bodies / tokens.
        super().log_message(fmt, *args)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def authorized_origin(self):
        # Protect local unauthenticated API against browser cross-origin requests / DNS rebinding.
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.server.server_port == 80:
            allowed.update({'127.0.0.1', 'localhost'})
        if host not in allowed:
            self.send_json(403, {'error': '管理介面僅接受 localhost / 127.0.0.1 存取'})
            return False
        origin = self.headers.get('Origin')
        if (origin and origin != 'http://' + host) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send_json(403, {'error': '不允許跨來源請求'})
            return False
        return True

    def do_GET(self):
        if not self.authorized_origin():
            return
        path = urlsplit(self.path).path
        if path == '/api/config/export':
            return self.send_json(200, self.server.monitor.export_config())
        if path == '/api/state':
            return self.send_json(200, self.server.monitor.state())
        if path == '/api/settings':
            return self.send_json(200, self.server.monitor.get_settings())
        if path == '/api/notifications':
            return self.send_json(200, self.server.monitor.notifications.public())
        if path == '/api/scan':
            with self.server.monitor.lock:
                snapshot = copy.deepcopy(self.server.monitor.scan)
            return self.send_json(200, snapshot)
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/static/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path not in files:
            return self.send_json(404, {'error': '找不到資源'})
        filename, mime = files[path]
        try:
            body = (ROOT / 'static' / filename).read_bytes()
        except FileNotFoundError:
            return self.send_json(404, {'error': '找不到前端檔案'})
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def mutate(self):
        if not self.authorized_origin():
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' and self.command != 'DELETE':
                raise ValueError('Content-Type 須為 application/json')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 32768:
                raise ValueError('請求內容過大')
            data = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(data, dict):
                raise ValueError('請求須為 JSON 物件')
            path = urlsplit(self.path).path
            monitor = self.server.monitor
            if path == '/api/settings' and self.command == 'PUT':
                return self.send_json(200, monitor.update_settings(data))
            if path == '/api/notifications' and self.command == 'PUT':
                return self.send_json(200, monitor.notifications.update(data))
            if path == '/api/notifications/test' and self.command == 'POST':
                return self.send_json(202, monitor.notifications.test())
            if path == '/api/hosts' and self.command == 'POST':
                return self.send_json(201, monitor.change(data))
            if path.startswith('/api/hosts/') and self.command in ('PUT', 'DELETE'):
                return self.send_json(200, monitor.change(data, path[len('/api/hosts/'):], self.command == 'DELETE'))
            if self.command == 'POST' and (match := re.fullmatch(r'/api/hosts/([^/]+)/wake', path)):
                if data:
                    raise ValueError('喚醒請求不接受額外參數')
                try:
                    monitor.wake_host(match[1])
                except OSError:
                    return self.send_json(502, {'error': '無法送出喚醒封包，請檢查管理端網路'})
                return self.send_json(200, {'ok': True})
            if path == '/api/refresh' and self.command == 'POST':
                monitor.wake.set()
                return self.send_json(202, {'ok': True})
            if path == '/api/scan' and self.command == 'POST':
                return self.send_json(202, monitor.start_scan(data))
            self.send_json(404, {'error': '找不到 API'})
        except KeyError as exc:
            self.send_json(404, {'error': str(exc)})
        except (ValueError, TypeError, RecursionError) as exc:
            self.send_json(400, {'error': str(exc)})
        except OSError:
            self.send_json(500, {'error': '無法儲存設定，請檢查資料目錄權限'})

    do_POST = mutate
    do_PUT = mutate
    do_DELETE = mutate


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CLIENTS
    request_deadline = REQUEST_DEADLINE

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
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

    def server_close(self):
        if hasattr(self, 'monitor'):
            self.monitor.notifications.close()
        super().server_close()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--bind', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1', help='Use 0.0.0.0 only behind a loopback-only Docker published port')
    parser.add_argument('--interval', type=int, default=30)
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    args = parser.parse_args()
    if not 5 <= args.interval <= 3600:
        parser.error('--interval must be between 5 and 3600 seconds')
    monitor = Monitor(args.data_dir, args.interval)
    server = Server((args.bind, port(args.port)), Handler)
    server.monitor = monitor
    threading.Thread(target=monitor.loop, daemon=True).start()
    print(f'Netword monitor: http://127.0.0.1:{args.port} (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        monitor.wake.set()
        server.server_close()


if __name__ == '__main__':
    main()
