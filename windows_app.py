"""Per-user Windows WebView2 desktop app; never shares Docker configuration."""
import argparse
import csv
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

APP_NAME = 'Netword 主機監控'
URL = 'http://127.0.0.1:8080'


def app_directory():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise RuntimeError('LOCALAPPDATA is unavailable')
    return Path(base) / 'NetwordMonitor'


def prepare_directory(directory):
    """Remove inherited access before creating any logs or private settings."""
    directory.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                            text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    sid = next(csv.reader([result.stdout.strip()]))[-1]
    if not re.fullmatch(r'S-1-[0-9-]+', sid):
        raise RuntimeError('Cannot determine current user SID')
    subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r',
                    f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'],
                   capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    (directory / 'data').mkdir(exist_ok=True)
    (directory / 'logs').mkdir(exist_ok=True)


class InstanceLock:
    """An OS file lock also covers another login session of this user."""
    def __init__(self, directory):
        import msvcrt
        self.stream = (directory / 'application.lock').open('a+b')
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b'0')
                self.stream.flush()
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.stream.close()
            raise RuntimeError('Application already running') from None

    def close(self):
        self.stream.close()


def kernel32():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateEventW.restype = wintypes.HANDLE
    api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenEventW.restype = wintypes.HANDLE
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.SetEvent.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


class NamedEvent:
    def __init__(self, name=None):
        self.name = name or 'Local\\NetwordMonitor-' + uuid.uuid4().hex
        self.api = kernel32()
        self.handle = (self.api.OpenEventW(0x100002, False, name) if name else
                       self.api.CreateEventW(None, True, False, self.name))
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def set(self):
        if not self.api.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, milliseconds):
        result = self.api.WaitForSingleObject(self.handle, milliseconds)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class LogStream:
    """Bounded on-disk logs, including HTTP handler messages."""
    def write(self, message):
        if message.strip():
            logging.info('%s', message.rstrip())
        return len(message)

    def flush(self):
        pass


def configure_logging(directory):
    handler = RotatingFileHandler(directory / 'logs' / 'application.log',
                                  maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    sys.stdout = sys.stderr = LogStream()


def run_backend(directory, stop_name, ready_name, port=8080, parent_pid=0, session=''):
    import server
    prefix = '/__desktop__/' + session if session else ''
    class DesktopHandler(server.Handler):
        def authorized_origin(self):
            if not super().authorized_origin():
                return False
            if prefix and self.path.startswith(prefix + '/'):
                self.path = self.path[len(prefix):]
            elif prefix and self.path.startswith('/api/'):
                self.send_json(404, {'error': '桌面工作階段已結束'})
                return False
            return True

        def do_GET(self):
            resources = {
                prefix + '/': ('index.html', 'text/html; charset=utf-8'),
                prefix + '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                prefix + '/static/style.css': ('style.css', 'text/css; charset=utf-8'),
            }
            if prefix and self.path in resources:
                filename, mime = resources[self.path]
                if not self.authorized_origin():
                    return
                body = (server.ROOT / 'static' / filename).read_bytes()
                if filename == 'index.html':
                    # Scope assets as well: replacement Docker must never supply its JS.
                    body = body.replace(b'"/static/', ('"' + prefix + '/static/').encode())
                elif filename == 'app.js':
                    marker = b'fetch(path, options)'
                    if body.count(marker) != 1:
                        self.send_json(500, {'error': '桌面介面版本不符'})
                        return
                    body = body.replace(marker, ('fetch("' + prefix + '" + path, options)').encode())
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()
    class DesktopServer(server.Server):
        # Windows SO_REUSEADDR can let two listeners bind the same port.
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    configure_logging(directory)
    stop_event, ready_event = NamedEvent(stop_name), NamedEvent(ready_name)
    httpd = monitor = None
    try:
        httpd = DesktopServer(('127.0.0.1', port), DesktopHandler)
        monitor = server.Monitor(directory / 'data')
        httpd.monitor = monitor
        threading.Thread(target=monitor.loop, daemon=True, name='host-monitor').start()
        def wait_for_stop():
            stop_event.wait(0xFFFFFFFF)
            monitor.stop.set()
            monitor.wake.set()
            httpd.shutdown()
        threading.Thread(target=wait_for_stop, daemon=True, name='desktop-stop').start()
        if parent_pid:
            def watch_parent():
                api = kernel32()
                handle = api.OpenProcess(0x100000, False, parent_pid)
                try:
                    if handle:
                        api.WaitForSingleObject(handle, 0xFFFFFFFF)
                    stop_event.set()
                    # Ensure abandoned network workers cannot outlive the tray indefinitely.
                    time.sleep(10)
                    os._exit(0)
                finally:
                    if handle:
                        api.CloseHandle(handle)
            threading.Thread(target=watch_parent, daemon=True, name='desktop-parent').start()
        ready_event.set()
        logging.info('管理端已啟動，僅綁定 127.0.0.1:%s', port)
        httpd.serve_forever(poll_interval=0.2)
        return 0
    except OSError as error:
        if getattr(error, 'winerror', None) == 10048 or getattr(error, 'errno', None) == 10048:
            logging.error('啟動失敗：8080 已被其他程式使用。請勿同時啟動 Docker 與桌面管理端。')
        else:
            logging.error('啟動失敗：檔案或網路作業失敗（%s），請檢查資料目錄權限。', type(error).__name__)
        return 1
    except Exception as error:
        # Never log exception text: it may contain configuration secrets.
        logging.error('啟動失敗：設定無法讀取或服務無法啟動（%s）。', type(error).__name__)
        return 1
    finally:
        if monitor is not None:
            monitor.stop.set()
            monitor.wake.set()
            monitor.notifications.close()
        if httpd is not None:
            httpd.server_close()
        stop_event.close()
        ready_event.close()


def backend_command(directory, stop_name, ready_name, port=8080, session=''):
    executable = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve())]
    return executable + ['--backend', '--directory', str(directory), '--stop-event', stop_name,
                         '--ready-event', ready_name, '--port', str(port), '--parent-pid', str(os.getpid()),
                         '--session', session]


class BackendController:
    def __init__(self, directory, port=8080):
        self.directory = directory
        self.port = port
        self.session = uuid.uuid4().hex
        self.url = f'http://127.0.0.1:{port}/__desktop__/{self.session}'
        self.process = None
        self.stop_event = self.ready_event = None
        self.lock = threading.Lock()

    def running(self):
        return self.process is not None and self.process.poll() is None

    def _close_events(self):
        for event in (self.stop_event, self.ready_event):
            if event is not None:
                event.close()
        self.stop_event = self.ready_event = None

    def start(self):
        with self.lock:
            if self.running():
                return
            self._close_events()
            self.stop_event, self.ready_event = NamedEvent(), NamedEvent()
            try:
                self.process = subprocess.Popen(backend_command(self.directory, self.stop_event.name, self.ready_event.name, self.port, self.session),
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and self.running():
                    if self.ready_event.wait(100):
                        return
                raise RuntimeError('Backend did not become ready')
            except Exception:
                if self.running():
                    self.process.kill()
                    self.process.wait()
                self._close_events()
                raise

    def stop(self):
        with self.lock:
            try:
                if self.running():
                    self.stop_event.set()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
            finally:
                self._close_events()
                self.process = None


def message(text):
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)


def require_webview2(on_session_end=None):
    # pywebview 6.2.1 can silently choose MSHTML even with gui='edgechromium'.
    from webview.guilib import initialize
    if initialize('edgechromium').renderer != 'edgechromium':
        raise RuntimeError('WebView2 Runtime required')
    if on_session_end is not None:
        from Microsoft.Win32 import SystemEvents
        def session_ending(sender, args):
            on_session_end()  # Do not veto Windows shutdown/logoff with close-to-tray.
        SystemEvents.SessionEnding += session_ending
        def unsubscribe():
            SystemEvents.SessionEnding -= session_ending
        return unsubscribe


def run_tray(directory):
    import pystray
    import webview
    from PIL import Image
    controller = BackendController(directory)
    resources = Path(__file__).resolve().parent / 'packaging'
    image = Image.open(resources / 'netword.ico')
    window = None
    exiting = threading.Event()
    release_session = None
    def open_page(icon=None, item=None):
        if window is not None:
            window.show()
            window.restore()
    def closing():
        if not exiting.is_set():
            window.hide()
            return False  # Close to tray; monitoring continues.
    def start(icon=None, item=None):
        try:
            controller.start()
        except Exception:
            message('管理端無法啟動。8080 可能已被 Docker 或其他程式占用，或設定無法讀取。\n'
                    '未變更任何既有設定，請查看「開啟日誌資料夾」。')
        if icon:
            icon.update_menu()
    def stop(icon, item):
        controller.stop()
        icon.update_menu()
    def quit_app(icon, item):
        exiting.set()
        if window is not None:
            window.destroy()
    icon = pystray.Icon('NetwordMonitor', image, APP_NAME, pystray.Menu(
        pystray.MenuItem('開啟管理介面', open_page, default=True),
        pystray.MenuItem(lambda item: '監控中' if controller.running() else '監控已停止', None, enabled=False),
        pystray.MenuItem('啟動監控', start, enabled=lambda item: not controller.running()),
        pystray.MenuItem('停止監控', stop, enabled=lambda item: controller.running()),
        pystray.MenuItem('開啟資料資料夾', lambda icon, item: os.startfile(str(directory / 'data'))),
        pystray.MenuItem('開啟日誌資料夾', lambda icon, item: os.startfile(str(directory / 'logs'))),
        pystray.MenuItem('離開', quit_app)))
    try:
        release_session = require_webview2(exiting.set)
        start()
        if not controller.running():
            return  # Never display another application's listener (including Docker).
        # No Python API bridge is exposed to page content.
        window = webview.create_window(APP_NAME, controller.url + '/', width=1280, height=820,
                                       min_size=(760, 560), text_select=True)
        window.events.closing += closing
        icon.run_detached()
        # WebView2 must run on the main thread; never fall back to legacy MSHTML.
        webview.start(gui='edgechromium', debug=False, private_mode=True)
    except Exception:
        message('桌面視窗無法啟動。請確認已安裝 Microsoft Edge WebView2 Runtime，並重新啟動程式。\n'
                '下載：https://developer.microsoft.com/microsoft-edge/webview2/\n'
                '未變更任何既有設定。')
        raise
    finally:
        exiting.set()
        if release_session is not None:
            release_session()
        icon.stop()
        controller.stop()
        image.close()


def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows only')
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument('--backend', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--directory', help=argparse.SUPPRESS)
    parser.add_argument('--stop-event', help=argparse.SUPPRESS)
    parser.add_argument('--ready-event', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=8080, help=argparse.SUPPRESS)
    parser.add_argument('--parent-pid', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--session', default='', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.backend:
        if not all((args.directory, args.stop_event, args.ready_event)):
            return 1
        return run_backend(Path(args.directory), args.stop_event, args.ready_event, args.port, args.parent_pid, args.session)
    directory = app_directory()
    instance = None
    try:
        prepare_directory(directory)
        instance = InstanceLock(directory)
        run_tray(directory)
    except Exception as error:
        if instance is None and isinstance(error, RuntimeError) and str(error) == 'Application already running':
            message('程式已在執行，請查看系統匣圖示（含隱藏的圖示）。')
        else:
            message('桌面程式無法啟動，請確認安裝完整且使用者資料目錄可寫入。')
        return 1
    finally:
        if instance is not None:
            instance.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
