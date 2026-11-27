import json
import tempfile
import unittest

from config_import import parse_export, prepare_import
import server
from notifications import DEFAULTS


def export(hosts=(), **notification):
    return {'format': 'netword-monitor', 'version': 1,
            'hosts': list(hosts), 'settings': {'refresh_interval': 6, 'monitor_interval': 33},
            'notifications': dict({k: v for k, v in DEFAULTS.items() if k != 'password'}, **notification)}


def prepare(document, current):
    return prepare_import(parse_export(json.dumps(document).encode()), current, normalize_ip=server.valid_ip,
                          validate_host=server.validate_host, validate_frequency=server.validate_settings)


class ImportValidationTests(unittest.TestCase):
    def test_accepts_export_and_pre_docker_v1(self):
        with tempfile.TemporaryDirectory() as path:
            monitor = server.Monitor(path)
            old = monitor.change({'ip': '127.0.0.1', 'name': '舊主機'})
            second = monitor.change({'ip': '127.0.0.2'})
            document = monitor.export_config()
            current = snapshot(monitor)
            candidate, summary = prepare(document, current)
            self.assertEqual(candidate, current)
            self.assertEqual(summary['hosts']['unchanged'], 2)
            host = document['hosts'][0]
            for key in ('docker_monitor_enabled', 'website_monitor_enabled', 'mac_address'):
                host.pop(key)
            host['name'] = '新名稱'
            document['hosts'][1]['ip'] = '127.0.0.3'
            candidate, summary = prepare(document, current)
            self.assertEqual(candidate['hosts'][0]['id'], old['id'])
            self.assertNotEqual(candidate['hosts'][1]['id'], second['id'])
            self.assertIs(candidate['hosts'][0]['docker_monitor_enabled'], False)
            self.assertEqual(summary['hosts']['removed_hosts'], [{'ip': '127.0.0.2', 'name': '127.0.0.2'}])
            candidate, summary = prepare(export(), current)
            self.assertEqual(candidate['hosts'], [])
            self.assertEqual(candidate['settings']['refresh_interval'], 6)
            self.assertEqual(summary['hosts']['removed'], 2)

    def test_rejects_ambiguous_or_secret_documents(self):
        encoded = json.dumps(export()).encode()
        self.assertEqual(parse_export(b'\xef\xbb\xbf' + encoded), export())
        for raw in (b'\xff', encoded.replace(b'"version": 1', b'"version": true'),
                    encoded.replace(b'"version": 1', b'"version": 2'),
                    encoded.replace(b'"refresh_interval": 6', b'"refresh_interval": 6, "refresh_interval": 7'),
                    *(encoded.replace(b'"refresh_interval": 6', b'"refresh_interval": ' + x)
                      for x in (b'NaN', b'Infinity', b'1e999', b'"\\ud800"'))):
            with self.subTest(raw=raw[:90]), self.assertRaises(ValueError):
                parse_export(raw)
        current = dict(hosts=[], settings={}, notifications=dict(DEFAULTS))
        valid = {'ip': '127.0.0.1'}
        for document in (
            export([dict(valid, agent_token='do-not-echo')]), export(password='do-not-echo'),
            export(password_set=True), export(delivery={}),
            export([valid, valid]), export([dict(valid, checks=[{'type': 'http', 'url': 'https://example.com/'}])]),
            export([{'ip': '192.168.6.23', 'agent_enabled': False, 'docker_monitor_enabled': True}]),
            export(smtp_host='bad host'), export(enabled=True, username='example'),
        ):
            with self.assertRaises(ValueError) as err:
                prepare(document, current)
            self.assertNotIn('do-not-echo', str(err.exception))

    def test_matches_credentials_to_connections_only(self):
        token, password = 'a' * 24, 'secret-smtp-password'
        current = {
            'hosts': [server.validate_host({'ip': '192.168.6.23', 'agent_enabled': True, 'agent_token': token})],
            'settings': {}, 'notifications': dict(DEFAULTS, username='person@gmail.com', password=password),
        }
        host = {'ip': '192.168.6.23', 'agent_enabled': True, 'docker_monitor_enabled': True,
                'website_monitor_enabled': True}
        notify = dict(enabled=True, smtp_host='SMTP.GMAIL.COM', username='person@gmail.com',
                      sender='person@gmail.com', recipients=['receiver@example.com'])
        candidate, summary = prepare(export([host], **notify), current)
        self.assertEqual(candidate['hosts'][0]['agent_token'], token)
        self.assertEqual(candidate['notifications']['password'], password)
        self.assertEqual(summary['agent_credentials_needed'], [])
        self.assertFalse(summary['smtp_password_needed'])
        self.assertNotIn(password, repr(summary))
        self.assertNotIn(token, repr(summary))
        for overrides in ({'smtp_port': 465}, {'smtp_host': 'mail.example.com'},
                          {'username': 'other@gmail.com'}, {'security': 'ssl'}):
            candidate, summary = prepare(export([dict(host, agent_port=9106)], **(notify | overrides)), current)
            result = candidate['hosts'][0]
            self.assertFalse(result['docker_monitor_enabled'])
            self.assertFalse(result['agent_enabled'])
            self.assertTrue(result['website_monitor_enabled'])
            self.assertEqual(result['agent_token'], '')
            self.assertTrue(summary['agent_credentials_needed'][0]['docker_was_enabled'])
            self.assertEqual(candidate['notifications']['password'], '')
            self.assertFalse(candidate['notifications']['enabled'])
            self.assertTrue(summary['smtp_password_needed'])
        candidate, summary = prepare(export(**(notify | {'smtp_host': 'mail.example.com', 'username': ''})), current)
        self.assertTrue(candidate['notifications']['enabled'])
        self.assertFalse(summary['smtp_password_needed'])


def snapshot(monitor):
    return {'hosts': monitor.hosts, 'settings': monitor.get_settings(), 'notifications': monitor.notifications.settings}
