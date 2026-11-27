"""Validate portable v1 settings without trusting any imported credentials."""
import json
import math

from notifications import DEFAULTS, validate_settings as validate_notifications


IMPORT_FILE_LIMIT = 40 * 1024 * 1024
HOST_FIELDS = frozenset(('id', 'ip', 'name', 'checks', 'agent_enabled', 'agent_port',
                         'mac_address', 'docker_monitor_enabled', 'website_monitor_enabled'))
NOTIFICATION_FIELDS = DEFAULTS.keys() - {'password'}
CHECK_FIELDS = {'tcp': {'type', 'port'}, 'http': {'type', 'url'}}
SECTIONS = {'format', 'version', 'hosts', 'settings', 'notifications'}


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON 含有重複欄位')
        result[key] = value
    return result


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('JSON 含有非有限數值')
    return number


def parse_export(raw: bytes) -> dict:
    if len(raw) > IMPORT_FILE_LIMIT:
        raise ValueError('設定檔不可超過 40 MiB')
    try:
        document = load_configuration_json(raw.decode('utf-8-sig'))
    except (ValueError, RecursionError):
        raise ValueError('設定檔不是有效的 UTF-8 JSON，或含有重複欄位或無效數值') from None
    if not isinstance(document, dict) or document.keys() != SECTIONS:
        raise ValueError('設定檔必須包含主機、更新頻率與郵件設定，不接受額外欄位')
    if document['format'] != 'netword-monitor' or type(document['version']) is not int or document['version'] != 1:
        raise ValueError('不支援此設定檔格式或版本')
    if not isinstance(document['hosts'], list) or len(document['hosts']) > 256:
        raise ValueError('主機清單格式錯誤，最多 256 台')
    for i, host in enumerate(document['hosts'], 1):
        if (not isinstance(host, dict) or host.keys() - HOST_FIELDS or not isinstance(host.get('ip'), str)
                or ('id' in host and (not isinstance(host['id'], str) or not 1 <= len(host['id']) <= 128))):
            raise ValueError(f'第 {i} 筆主機含有不支援的欄位或格式')
        checks = host.get('checks', [])
        if not isinstance(checks, list) or len(checks) > 16:
            raise ValueError(f'第 {i} 筆主機的服務檢查格式錯誤')
        for check in checks:
            kind = check.get('type') if isinstance(check, dict) else None
            if not isinstance(kind, str) or kind not in CHECK_FIELDS or check.keys() != CHECK_FIELDS[kind]:
                raise ValueError(f'第 {i} 筆主機含有不支援的服務檢查')
    frequencies = document['settings']
    if not isinstance(frequencies, dict) or frequencies.keys() - {'refresh_interval', 'monitor_interval'}:
        raise ValueError('更新頻率含有不支援的欄位或格式')
    notifications = document['notifications']
    if not isinstance(notifications, dict) or notifications.keys() - NOTIFICATION_FIELDS:
        raise ValueError('郵件設定含有不支援的欄位，不可匯入 SMTP 密碼')
    return document


def load_configuration_json(text: str):
    document = json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_finite, parse_float=_finite)
    # Escaped lone surrogates can survive strict UTF-8 decoding of the file.
    _validate_text(document)
    return document


def _validate_text(value):
    if isinstance(value, str):
        value.encode('utf-8')
    elif isinstance(value, list):
        for item in value:
            _validate_text(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            key.encode('utf-8')
            _validate_text(item)


def _public_host(host):
    return {'ip': host['ip'], 'name': host['name']}


def _smtp_destination(settings):
    return settings['smtp_host'].lower(), settings['smtp_port'], settings['security']


def prepare_import(document, current, *, normalize_ip, validate_host, validate_frequency) -> tuple[dict, dict]:
    old_hosts = {host['ip']: host for host in current['hosts']}
    seen = set()
    hosts, missing_agents = [], []
    counts = {'added': 0, 'updated': 0, 'removed': 0, 'unchanged': 0}
    for i, record in enumerate(document['hosts'], 1):
        try:
            ip = normalize_ip(record['ip'])
            old = old_hosts.get(ip)
            # Use only the local ID and a credential bound to the same port;
            # no other omitted value should be merged from the old host.
            host = validate_host(record, {'id': old['id']} if old else None, allow_missing_token=True)
        except (ValueError, TypeError):
            raise ValueError(f'第 {i} 筆主機設定無效，請檢查 IP、服務檢查與監控開關') from None
        if ip in seen:
            raise ValueError(f'第 {i} 筆主機的 IP 重複')
        seen.add(ip)
        if old and old['agent_port'] == host['agent_port']:
            host['agent_token'] = old['agent_token']
        if host['agent_enabled'] and len(host['agent_token']) < 24:
            missing_agents.append({**_public_host(host), 'docker_was_enabled': host['docker_monitor_enabled']})
            host.update(agent_enabled=False, docker_monitor_enabled=False)
        counts['added' if old is None else 'unchanged' if old == host else 'updated'] += 1
        hosts.append(host)
    counts['removed_hosts'] = [_public_host(host) for ip, host in old_hosts.items() if ip not in seen]
    counts['removed'] = len(counts['removed_hosts'])
    try:
        settings = validate_frequency(document['settings'])
    except (ValueError, TypeError):
        raise ValueError('更新頻率設定無效') from None
    old_notice = current['notifications']
    try:
        notice = validate_notifications(document['notifications'], allow_missing_password=True)
    except (ValueError, TypeError):
        raise ValueError('郵件通知設定無效，請檢查 SMTP 與收件人設定') from None
    target_changed = _smtp_destination(notice) != _smtp_destination(old_notice)
    account_changed = notice['username'] != old_notice['username']
    if not target_changed and not account_changed:
        notice['password'] = old_notice['password']
    missing_password = bool(notice['enabled'] and notice['username'] and not notice['password'])
    if missing_password:
        notice['enabled'] = False
    notice = validate_notifications(notice)
    summary = {
        'hosts': counts, 'settings': dict(settings),
        'notifications': {**{key: notice[key] for key in ('enabled', 'smtp_host', 'smtp_port', 'security', 'recipients')},
                          'target_changed': target_changed,
                          'account_changed': account_changed,
                          'recipients_changed': notice['recipients'] != old_notice['recipients']},
        'agent_credentials_needed': missing_agents, 'smtp_password_needed': missing_password,
    }
    return {'hosts': hosts, 'settings': settings, 'notifications': notice}, summary
