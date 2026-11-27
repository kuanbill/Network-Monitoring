"""Private SMTP settings and bounded, asynchronous host-incident notifications."""
import copy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import json
import os
from pathlib import Path
import queue
import re
import smtplib
import socket
import ssl
import tempfile
import threading
import time
from website_probe import WEBSITE_DOMAINS

DEFAULTS = {
    'enabled': False, 'smtp_host': 'smtp.gmail.com', 'smtp_port': 587,
    'security': 'starttls', 'username': '', 'password': '', 'sender': '',
    'recipients': [], 'notify_recovery': True, 'failure_threshold': 2,
    'recovery_threshold': 2,
}
QUEUE_SIZE = 256
DELIVERY_DEADLINE = 20
RETRY_DELAY = 60
MAX_ATTEMPTS = 3
INCIDENT_TYPES = ('host', 'docker', 'website')
DOCKER_UNIT = 'docker.service'


class DeliveryUncertain(Exception):
    """The connection failed during DATA; SMTP may already have accepted it."""


def validate_settings(data, previous=None, complete=False):
    if not isinstance(data, dict):
        raise ValueError('通知設定須為 JSON 物件')
    settings = copy.deepcopy(DEFAULTS if previous is None else previous)
    for key in DEFAULTS:
        if key in data:
            settings[key] = data[key]
    for key in ('enabled', 'notify_recovery'):
        if not isinstance(settings[key], bool):
            raise ValueError('通知開關須為布林值')
    for key, maximum in (('smtp_host', 253), ('username', 254), ('password', 1024), ('sender', 254)):
        value = settings[key]
        if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError('SMTP 欄位須為有效 ASCII 文字，不可含換行')
        if key != 'password':
            settings[key] = value.strip()
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', settings['smtp_host']):
        raise ValueError('SMTP 主機名稱格式錯誤')
    number = settings['smtp_port']
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 65535:
        raise ValueError('SMTP 連接埠須為 1–65535 的整數')
    if settings['security'] not in ('starttls', 'ssl'):
        raise ValueError('SMTP 必須使用 STARTTLS 或 TLS 加密')
    for key in ('failure_threshold', 'recovery_threshold'):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
            raise ValueError('連續檢查次數須為 1–10 的整數')
    recipients = settings['recipients']
    if not isinstance(recipients, list) or len(recipients) > 20:
        raise ValueError('收件人須為清單，最多 20 人')
    def address(value):
        # Deliberately accept only a bare, ASCII mailbox (no display name or headers).
        return (isinstance(value, str) and len(value) <= 254 and
                re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value))
    if any(not address(value) for value in recipients) or (settings['sender'] and not address(settings['sender'])):
        raise ValueError('請輸入純 Email 位址，不可含顯示名稱、空白或換行')
    settings['recipients'] = list(dict.fromkeys(recipients))
    if settings['enabled'] or complete:
        if not settings['sender'] or not settings['recipients']:
            raise ValueError('請設定寄件人及至少一位收件人')
        if settings['smtp_host'].lower() == 'smtp.gmail.com' and not settings['username']:
            raise ValueError('Gmail 請設定完整登入 Email 及應用程式密碼')
        if settings['username'] and not settings['password']:
            raise ValueError('SMTP 登入帳號需要密碼；Gmail 請使用應用程式密碼')
    return settings


def send_email(settings, subject, body):
    """Verified TLS only; a timer interrupts trickling SMTP commands/data."""
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = settings['sender']
    # Do not disclose the recipient list to the other recipients.
    message['To'] = 'undisclosed-recipients:;'
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid(domain='lanscope.local')
    message.set_content(body)
    context = ssl.create_default_context()
    client = (smtplib.SMTP_SSL(timeout=5, context=context, local_hostname='lanscope.local')
              if settings['security'] == 'ssl' else smtplib.SMTP(timeout=5, local_hostname='lanscope.local'))
    # Python 3.10–3.13 connect() does not update the TLS verification hostname.
    # Set it before connecting; construction without a host keeps the timer in scope.
    client._host = settings['smtp_host']
    expired = threading.Event()
    def expire():
        expired.set()
        transport = client.sock
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()
    def check_deadline():
        if expired.is_set():
            raise TimeoutError('SMTP delivery deadline')
    timer = threading.Timer(DELIVERY_DEADLINE, expire)
    timer.daemon = True
    timer.start()
    try:
        client.connect(settings['smtp_host'], settings['smtp_port'])
        check_deadline()
        client.ehlo()
        if settings['security'] == 'starttls':
            check_deadline()
            client.starttls(context=context)
            client.ehlo()
        check_deadline()
        if settings['username']:
            client.login(settings['username'], settings['password'])
        check_deadline()
        try:
            refused = client.send_message(message, from_addr=settings['sender'], to_addrs=settings['recipients'])
        except (OSError, smtplib.SMTPServerDisconnected):
            # An acknowledgement can be lost after DATA was accepted. Retrying
            # would duplicate the message; do not claim it definitely failed.
            raise DeliveryUncertain() from None
        if refused:
            # Some recipients may have accepted the message; avoid duplicate resends to them.
            return False
        return True
    finally:
        timer.cancel()
        try:
            client.close()
        except OSError:
            pass


def failure_explanation(detail, agent=False):
    """Translate known evidence only; never copy remote errors or private URLs."""
    text = str(detail).lower()
    status = re.fullmatch(r'(?:代理程式 )?http ([1-5][0-9]{2})', text)
    if status:
        code = int(status[1])
        if code in (401, 403):
            return ('認證未通過或存取遭拒；請檢查 Agent Token 與來源 IP 白名單。' if agent else
                    '網站拒絕存取或要求認證；請檢查網站權限與驗證設定。') + f'（HTTP {code}）'
        if code == 404:
            return f'找不到檢查端點；請確認服務版本與檢查路徑設定。（HTTP {code}）'
        if code == 503 and agent:
            return 'Agent 暫時無法提供主機指標，可能是指標收集逾時或資料過期；請檢查 Agent 日誌。（HTTP 503）'
        return f'服務回傳非預期狀態碼（HTTP {code}）；請檢查應用程式日誌與服務健康狀態。'
    if 'timed out' in text or 'timeout' in text or '逾時' in text:
        return '連線或回應逾時；可能是防火牆阻擋、網路中斷或服務忙碌，請檢查連線與服務日誌。'
    if 'refused' in text or '10061' in text or '拒絕' in text:
        return '連線遭拒；可能是服務未啟動、未監聽指定連接埠或防火牆主動拒絕，請檢查服務與連接埠。'
    if any(value in text for value in ('certificate', 'ssl', 'tls', '憑證')):
        return '加密連線或憑證驗證失敗；請檢查憑證有效期、信任鏈及憑證是否符合目標 IP。'
    if any(value in text for value in ('unreachable', 'no route', '10065', '10051')):
        return '無法到達目標網路或主機；請檢查路由、網路設備與主機連線。'
    if any(value in text for value in ('reset', 'closed', 'disconnected', '10054')):
        return '連線遭中斷或對端提前關閉；請檢查服務日誌、網路設備與防火牆。'
    if agent and any(value in text for value in ('json', '指標', '格式', '非有限', 'unicode', 'utf-8')):
        return 'Agent 回應無法解析或指標格式不符；請檢查 Agent 版本與指標收集日誌。'
    return ('無法取得 Agent 指標；請檢查 Agent 是否運行、連接埠、Token 與來源 IP 限制。' if agent else
            '檢查未成功，現有資料不足以確認原因；請檢查服務是否運行、監聽連接埠及防火牆。')


class NotificationManager:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'notifications.json'
        self.lock = threading.RLock()
        self.settings = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            self.settings = validate_settings(json.loads(self.path.read_text(encoding='utf-8')))
        self.generation = 0
        self.records = {}
        self.host_versions = {}
        self.jobs = queue.Queue(maxsize=QUEUE_SIZE)
        self.stop = threading.Event()
        self.worker = None
        self.last_test = None
        self.delivery = {'status': 'idle', 'timestamp': None, 'error': ''}

    def public(self):
        with self.lock:
            result = {key: copy.deepcopy(value) for key, value in self.settings.items() if key != 'password'}
            result['password_set'] = bool(self.settings['password'])
            result['delivery'] = dict(self.delivery, pending=self.jobs.qsize())
            return result

    def update(self, data):
        with self.lock:
            settings = validate_settings(data, self.settings)
            if settings == self.settings:
                return self.public()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='notifications-', suffix='.tmp', dir=self.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(settings, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            # Commit only after persistence succeeded. Old queued jobs become invalid.
            self.settings = settings
            self.generation += 1
            self.records.clear()
            self.host_versions.clear()
            self.delivery = {'status': 'idle', 'timestamp': None, 'error': ''}
            return self.public()

    def _status(self, status, error=''):
        self.delivery = {'status': status, 'timestamp': time.time(), 'error': error}

    def _enqueue(self, kind, record=None, measurement=None):
        job = {'kind': kind, 'record': record, 'generation': self.generation,
               'settings': copy.deepcopy(self.settings)}
        if record is not None:
            job['version'] = self.host_versions.setdefault(record['id'], object())
            job['host'] = copy.deepcopy(record['host'])
            job['last'] = copy.deepcopy(record['last'] if measurement is None else measurement)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            self._status('failed', '通知佇列已滿，尚未寄出')
            return False
        if record is not None:
            record[kind + '_pending'] = True
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, daemon=True, name='email-notifications')
            self.worker.start()
        return True

    def test(self):
        with self.lock:
            validate_settings(self.settings, complete=True)
            now = time.monotonic()
            if self.last_test is not None and now - self.last_test < 30:
                raise ValueError('測試信每 30 秒最多寄送一次')
            if not self._enqueue('test'):
                raise ValueError('通知佇列已滿，請稍後重試')
            self.last_test = now
            self._status('queued')
            return {'ok': True, 'message': '測試信已排程；請查看寄送狀態與信箱'}

    def forget(self, identity, kinds: set[str] | None = None):
        with self.lock:
            for kind in INCIDENT_TYPES if kinds is None else kinds:
                self.records.pop((identity, kind), None)
                self.host_versions.pop((identity, kind), None)

    def observe(self, host, last):
        """One call per completed measurement, never per dashboard refresh."""
        with self.lock:
            if self.stop.is_set() or not self.settings['enabled']:
                return
            public_host = {'name': host['name'], 'ip': host['ip']}
            status = last.get('host_status', last.get('status'))
            if status in ('up', 'down', 'degraded'):
                agent = last.get('agent')
                if host.get('docker_monitor_enabled') and agent and agent.get('ok'):
                    metrics = agent.get('metrics', {})
                    services = [s for s in metrics.get('services', []) if s.get('name') != DOCKER_UNIT]
                    agent = {**agent, 'metrics': {'services': services}}
                host_sample = {'status': status, 'timestamp': last['timestamp'],
                               'checks': last.get('checks', []), 'agent': agent}
                self._observe((host['id'], 'host'), public_host, host_sample, status == 'up')
            for kind in ('docker', 'website'):
                if host.get(f'{kind}_monitor_enabled'):
                    check = last.get(kind) or {}
                    self._observe((host['id'], kind), public_host,
                                  {'timestamp': last['timestamp'], 'check': check}, check.get('ok'))

    def _observe(self, identity, host, last, ok):
        if ok is not True and ok is not False:
            record = self.records.get(identity)
            if record is not None:
                record['bad'] = record['good'] = 0
            return
        record = self.records.setdefault(identity, self._new_record(identity))
        record['host'] = host
        record['last'] = copy.deepcopy(last)
        if not ok:
            record['bad'] += 1
            record['good'] = 0
            if not record['active'] and record['bad'] >= self.settings['failure_threshold']:
                # Preserve a confirmed recovery even if the bounded queue was full.
                if (self.settings['notify_recovery'] and 'recovery_measurement' in record and
                        (record['alert_sent'] or record['alert_pending']) and
                        not record['recovery_sent'] and not record['recovery_pending'] and
                        record['recovery_attempts'] < MAX_ATTEMPTS):
                    if not self._enqueue('recovery', record, record['recovery_measurement']):
                        return
                renewed = self._new_record(identity)
                renewed.update(active=True, bad=record['bad'], host=record['host'], last=record['last'],
                               alert_measurement=copy.deepcopy(last))
                self.records[identity] = record = renewed
        else:
            record['bad'] = 0
            record['good'] += 1
            if record['active'] and record['good'] >= self.settings['recovery_threshold']:
                record['active'] = False
                record['recovery_measurement'] = copy.deepcopy(last)
        kind = 'alert' if record['active'] else 'recovery'
        eligible = record['active'] or ((record['alert_sent'] or record['alert_pending']) and
                                        self.settings['notify_recovery'] and 'recovery_measurement' in record)
        if (eligible and not record[kind + '_sent'] and not record[kind + '_pending'] and
                record[kind + '_attempts'] < MAX_ATTEMPTS and time.monotonic() >= record['retry_after']):
            self._enqueue(kind, record, record.get(kind + '_measurement'))

    @staticmethod
    def _new_record(identity):
        return {'id': identity, 'bad': 0, 'good': 0, 'active': False, 'alert_sent': False,
                'recovery_sent': False, 'alert_pending': False, 'recovery_pending': False,
                'alert_attempts': 0, 'recovery_attempts': 0, 'retry_after': 0}

    def _valid(self, job):
        if self.stop.is_set() or job['generation'] != self.generation:
            return False
        record = job['record']
        if record is None:
            return True
        if self.host_versions.get(record['id']) is not job['version']:
            return False
        return not record['alert_sent'] if job['kind'] == 'alert' else (record['alert_sent'] and not record['recovery_sent'])

    def _content(self, job):
        if job['kind'] == 'test':
            return '[LANscope] 郵件通知測試', '這是 LANscope 測試信。收到此信代表 SMTP 已接受本次寄送。'
        host, last = job['host'], job['last']
        label = '主機異常' if job['kind'] == 'alert' else '主機恢復'
        names = {'up': '運作正常', 'degraded': '部分異常', 'down': '無法連線（不代表已確認關機）'}
        # Host names are untrusted; never copy CR/LF into a mail header.
        name = ''.join(c if c.isprintable() else ' ' for c in host['name'])[:100]
        checked = time.strftime('%Y年%m月%d日 %H:%M:%S %z', time.localtime(last['timestamp']))
        category = job['record']['id'][1]
        if category != 'host':
            return self._independent_content(job, category, name, checked)
        lines = [f"主機：{name}", f"IP：{host['ip']}", f"狀態：{names[last['status']]}",
                 f"檢查時間：{checked}（管理端時區）"]
        if job['kind'] == 'alert':
            lines.extend([f"觸發條件：已連續 {job['settings']['failure_threshold']} 輪檢查異常。", '',
                          '異常項目、可能原因與建議：'])
            details = []
            if last['status'] == 'down':
                details.append('尚未取得主機可達證據；可能是主機離線、網路中斷或防火牆阻擋，不能據此確認主機已關機。請先檢查主機電源、網路及防火牆。')
            for check in last.get('checks', []):
                if not check.get('ok'):
                    check_label = '網頁回應檢查' if check.get('type') == 'http' else '服務連接埠檢查'
                    # Only a validated TCP port is safe to repeat; never include URLs.
                    port = re.fullmatch(r'TCP ([0-9]{1,5})', str(check.get('label', '')))
                    if check.get('type') == 'tcp' and port and 1 <= int(port[1]) <= 65535:
                        check_label += f'（TCP {int(port[1])}）'
                    details.append(check_label + '失敗：' + failure_explanation(check.get('detail', '')))
            agent = last.get('agent')
            if agent and not agent.get('ok'):
                details.append('主機代理程式（Agent）異常：' + failure_explanation(agent.get('error', ''), agent=True))
            service_states = {
                'stopped': '已停止；請確認是否應啟動此服務。',
                'inactive': '未運行；請確認是否應啟動此服務。',
                'failed': '啟動或執行失敗；請查看系統服務日誌。',
                'paused': '已暫停；請確認是否應恢復執行。',
                'activating': '正在啟動，尚未確認正常；請檢查是否長時間停留在啟動階段。',
                'deactivating': '正在停止；請確認是否為預期操作。',
                'reloading': '正在重新載入設定；請確認稍後能恢復正常。',
                'maintenance': '處於維護狀態；請檢查系統服務設定。',
                'start_pending': '正在啟動；請確認稍後能恢復正常。',
                'stop_pending': '正在停止；請確認是否為預期操作。',
                'pause_pending': '正在暫停；請確認是否為預期操作。',
                'continue_pending': '正在恢復執行；請確認稍後能恢復正常。',
            }
            for service in (agent or {}).get('metrics', {}).get('services', []):
                if service.get('status') not in ('running', 'active'):
                    service_name = ''.join(c if c.isprintable() else ' ' for c in service.get('name', ''))[:128]
                    explanation = service_states.get(service.get('status'), '無法確認狀態，不代表已確認停止；請確認服務名稱、存取權限與系統服務日誌。')
                    details.append(f'系統服務「{service_name}」：{explanation}')
            lines.extend(f'・{detail}' for detail in details or ['現有資料不足以確認異常項目；請至管理介面查看檢查結果。'])
            lines.extend(['', '以上為量測結果及可能原因，並非已確認的根本原因。'])
        else:
            lines.append(f"恢復說明：已連續 {job['settings']['recovery_threshold']} 輪檢查正常，本次異常事件已解除。")
        if job['kind'] == 'recovery' and job['record'].get('alert_uncertain'):
            lines.append('注意：先前異常通知的 SMTP 接受結果未能確認。')
        lines.extend(['', '同一異常事件不重複通知；確認恢復後才會建立下一次事件。',
                      '請開啟管理介面查看最新狀態。'])
        return f'[LANscope] {label}：{name} ({host["ip"]})', '\n'.join(lines)

    def _independent_content(self, job, category, name, checked):
        host, check = job['host'], job['last']['check']
        recovery = job['kind'] == 'recovery'
        title = 'Docker 服務' if category == 'docker' else '網站'
        state = '恢復' if recovery else '異常'
        threshold = job['settings']['recovery_threshold'] if recovery else 'failure_threshold'
        lines = [f"主機：{name}", f"IP：{host['ip']}", f'檢查時間：{checked}（管理端時區）',
                 f'已連續 {threshold} 輪檢查{"正常" if recovery else "異常"}。', '']
        if category == 'docker':
            lines.append(f'檢查項目：{title}（{DOCKER_UNIT}）')
            states = {
                'inactive': 'Docker 服務未運行；請確認這是否是預期操作。',
                'failed': 'Docker 服務回報失敗；請查看系統服務日誌。',
                'activating': 'Docker 服務正在啟動，尚未確認正常。',
                'deactivating': 'Docker 服務正在停止；請確認這是否是預期操作。',
                'reloading': 'Docker 服務正在重新載入。',
                'maintenance': 'Docker 服務處於維護狀態。',
            }
            lines.append('systemd 回報 Docker 服務運行中。' if recovery else states.get(
                check.get('code'), '無法確認 Docker 服務狀態；請確認 Agent 已設定檢查 docker.service。'))
            lines.append('此結果不代表容器或網站正常，請分別確認。')
        else:
            lines.append(f"檢查項目：網站 {WEBSITE_DOMAINS.get(host['ip'], '')}")
            status = check.get('http_status')
            errors = {
                'dns': 'DNS 查詢失敗或未取得公開位址；請檢查管理端的 DNS 及網站網域設定。',
                'certificate': 'HTTPS 憑證驗證失敗；請檢查有效期、網域及憑證鏈。',
                'connection': '無法取得 HTTPS 回應；請檢查管理端網路、反向代理及網站服務。',
                'timeout': '網站檢查逾時；請檢查 DNS、網路與網站回應時間。',
            }
            if check.get('code') == 'http' and type(status) is int and 100 <= status <= 599:
                lines.append(f'HTTPS 回應：HTTP {status}。')
                if not recovery:
                    lines.append('預期回傳 HTTP 2xx；請確認網站入口、重新導向設定與應用程式日誌。')
            elif not recovery:
                lines.append(errors.get(check.get('code'), '請至管理介面查看網站檢查結果。'))
            lines.append('此結果僅反映管理端所見的網站入口，不代表 Docker 狀態。')
        if recovery and job['record'].get('alert_uncertain'):
            lines.append('注意：先前異常通知的 SMTP 接受結果未能確認。')
        return f'[LANscope] {title}{state}：{name} ({host["ip"]})', '\n'.join(lines)

    def _run(self):
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(job)
            finally:
                self.jobs.task_done()

    def _process(self, job):
        # Retry in FIFO order: a recovery must not overtake its alert, nor can a
        # later incident overtake an already-confirmed recovery for the same host.
        record, kind = job['record'], job['kind']
        attempts = 1 if kind == 'test' else MAX_ATTEMPTS
        for attempt in range(attempts):
            with self.lock:
                if not self._valid(job):
                    break
                subject, body = self._content(job)
                self._status('sending')
            outcome = self._deliver(job, subject, body)
            with self.lock:
                if not self._valid(job):
                    if job['generation'] == self.generation and self.delivery['status'] in ('sending', 'timeout'):
                        self._status('idle')
                    break
                if outcome is None:
                    break
                status, error = outcome
                self._status(status, error if status != 'partial' else '部分收件人被拒絕；為避免重複寄信，不自動重寄')
                if record is not None:
                    record[kind + '_attempts'] += 1
                    record['retry_after'] = 0 if status in ('sent', 'partial', 'uncertain') else time.monotonic() + RETRY_DELAY
                if status in ('sent', 'partial', 'uncertain'):
                    if record is not None:
                        record[kind + '_sent'] = True
                        if kind == 'alert':
                            record['alert_uncertain'] = status == 'uncertain'
                    break
            if attempt + 1 < attempts and self.stop.wait(RETRY_DELAY):
                break
        with self.lock:
            if record is not None:
                record[kind + '_pending'] = False

    def _deliver(self, job, subject, body):
        # Even DNS or a misbehaving TLS library cannot exhaust workers: at most one
        # transport runs. After a deadline, report it but do not spawn a replacement
        # until the old transport actually exits (delivery outcome may be unknown).
        done = threading.Event()
        outcome = []
        def deliver():
            try:
                outcome.append(('sent' if send_email(job['settings'], subject, body) else 'partial', ''))
            except DeliveryUncertain:
                outcome.append(('uncertain', 'SMTP 接受結果未確定；為避免重複寄信，不自動重寄'))
            except Exception:
                # SMTP server errors may echo usernames, passwords or message content.
                outcome.append(('failed', '寄送失敗，請檢查 SMTP、應用程式密碼及網路設定'))
            finally:
                done.set()
        transport = threading.Thread(target=deliver, daemon=True, name='email-transport')
        transport.start()
        if not done.wait(DELIVERY_DEADLINE):
            with self.lock:
                if job['generation'] == self.generation:
                    self._status('timeout', 'SMTP 寄送逾時，結果未確定；等待原連線結束，不重複建立連線')
            while not done.wait(0.2):
                if self.stop.is_set():
                    return
        return outcome[0]

    def close(self):
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=0.5)
