"""Real WebView2/tray smoke test: temporary data and random port, no Docker changes.
Run with .venv-windows-build/Scripts/python.exe tests/windows_ui_smoke.py
"""
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_app as app


def main():
    import pystray
    import webview
    with tempfile.TemporaryDirectory(prefix='netword-ui-') as temporary:
        directory = Path(temporary)
        app.prepare_directory(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        controller = app.BackendController(directory, port)
        original_start = webview.start
        original_tray = pystray.Icon.run_detached
        icons, failures = [], []
        finished = threading.Event()

        def tray_start(icon, *args, **kwargs):
            icons.append(icon)
            return original_tray(icon, *args, **kwargs)

        def exercise():
            try:
                window = webview.windows[0]
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if window.evaluate_js('document.readyState') == 'complete':
                        break
                    time.sleep(0.1)
                assert window.evaluate_js('!!document.querySelector("#notification-dialog")')
                assert window.evaluate_js('location.origin') == f'http://127.0.0.1:{port}'
                # Native close must be cancelled and leave backend alive.
                window.destroy()
                time.sleep(0.5)
                assert controller.running()
                assert not finished.is_set()
                list(icons[0].menu)[0](icons[0])
                time.sleep(0.5)
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()
                list(icons[0].menu)[-1](icons[0])

        def ui_start(**kwargs):
            return original_start(exercise, **kwargs)

        def timeout():
            failures.append(RuntimeError('WebView2 smoke timed out'))
            finished.set()
            if icons:
                list(icons[0].menu)[-1](icons[0])

        timer = threading.Timer(40, timeout)
        timer.daemon = True
        timer.start()
        try:
            with patch.object(app, 'BackendController', return_value=controller), \
                    patch.object(app, 'URL', f'http://127.0.0.1:{port}'), \
                    patch.object(pystray.Icon, 'run_detached', tray_start), \
                    patch.object(webview, 'start', ui_start), \
                    patch('webbrowser.open', side_effect=AssertionError('External browser opened')):
                app.run_tray(directory)
            assert finished.is_set(), 'Window closed before checks completed'
            assert not controller.running()
            if failures:
                raise failures[0]
        finally:
            timer.cancel()
            controller.stop()
    print('PASS: real WebView2 page, close-to-tray, reopen, quit and backend cleanup')


if __name__ == '__main__':
    main()
