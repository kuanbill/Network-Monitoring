"""Exercise the real frozen executable/installer without touching production or port 8080.
Run: python tests/windows_package_smoke.py --installer dist/installer/NetwordMonitor-Setup-1.4.0-x64.exe
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_app as app


def exercise(executable, directory):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    controller = app.BackendController(directory, port)
    url = controller.url
    with patch.object(sys, 'executable', str(executable)), patch.object(sys, 'frozen', True, create=True):
        try:
            controller.start()
            with urllib.request.urlopen(url + '/', timeout=5) as response:
                html = response.read().decode()
                assert 'notification-dialog' in html
                assert 'settings-dialog' in html
                assert f'/__desktop__/{controller.session}/static/app.js' in html
            with urllib.request.urlopen(url + '/static/app.js', timeout=5) as response:
                assert controller.session.encode() in response.read()
            with urllib.request.urlopen(url + '/api/state', timeout=5) as response:
                assert json.load(response)['hosts'] == []
            payload = json.dumps({'name': 'Packaged smoke', 'ip': '127.0.0.1', 'checks': []}).encode()
            request = urllib.request.Request(url + '/api/hosts', data=payload,
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 201
            saved = (directory / 'data' / 'hosts.json').read_bytes()
            settings = {'refresh_interval': 2, 'monitor_interval': 60}
            request = urllib.request.Request(url + '/api/settings', data=json.dumps(settings).encode(),
                                             headers={'Content-Type': 'application/json'}, method='PUT')
            with urllib.request.urlopen(request, timeout=5) as response:
                assert json.load(response) == settings
            controller.stop()
            controller.start()
            with urllib.request.urlopen(url + '/api/state', timeout=5) as response:
                assert len(json.load(response)['hosts']) == 1
            assert (directory / 'data' / 'hosts.json').read_bytes() == saved
            with urllib.request.urlopen(url + '/api/settings', timeout=5) as response:
                assert json.load(response) == settings
        finally:
            controller.stop()
    assert (directory / 'logs' / 'application.log').exists()


def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows only')
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--exe', type=Path)
    group.add_argument('--installer', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='netword-package-') as temporary:
        root = Path(temporary)
        directory = root / 'profile'
        app.prepare_directory(directory)
        uninstaller = None
        try:
            if args.installer:
                # Do not overwrite an existing desktop install's uninstall registration.
                import winreg
                key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{D631C0F8-8DDA-4A62-9989-EE327257FB91}_is1'
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | view):
                            raise RuntimeError('Desktop app already installed; use --exe instead')
                    except FileNotFoundError:
                        pass
                install = root / 'installed app'
                subprocess.run([str(args.installer.resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                                '/NORESTART', '/TASKS=', '/DIR=' + str(install)], check=True, timeout=90)
                executable = install / 'NetwordMonitor.exe'
                uninstaller = install / 'unins000.exe'
            else:
                executable = args.exe.resolve()
            internal = executable.parent / '_internal'
            for relative in ('licenses/NOTICE.txt', 'licenses/WebView2-SDK/LICENSE.txt',
                             'webview/lib/Microsoft.Web.WebView2.Core.dll',
                             'webview/lib/Microsoft.Web.WebView2.WinForms.dll',
                             'pythonnet/runtime/Python.Runtime.dll',
                             'clr_loader/ffi/dlls/amd64/ClrLoader.dll'):
                assert (internal / relative).exists(), relative
            assert list((internal / 'webview').rglob('WebView2Loader.dll'))
            assert (internal / 'webview' / 'js').is_dir()
            exercise(executable, directory)
        finally:
            if uninstaller and uninstaller.exists():
                subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
                               check=True, timeout=60)
                # Inno's uninstaller removes its own files just after returning.
                import time
                for _ in range(50):
                    if not uninstaller.exists():
                        break
                    time.sleep(0.1)
                assert not executable.exists()
                assert (directory / 'data' / 'hosts.json').exists()
    print('PASS: isolated frozen backend, web resources, start/stop/restart and persistence'
          + ('; installer lifecycle' if args.installer else ''))


if __name__ == '__main__':
    main()
