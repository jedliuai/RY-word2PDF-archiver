"""Desktop shell: pywebview window (Edge WebView2) hosting the web/ front end.

Front end  -> `window.pywebview.api.*` (Api below) -> Controller -> existing workflow.
Back end   -> Controller.notify -> evaluate_js('window.__onState(...)').
No external browser is launched; WebView2 renders inside a native window.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

from app_controller import Controller

WEB_DIR = Path(__file__).resolve().parent / 'web'


class StatePusher:
    """Coalesce bursts of state updates into at most one JS call per interval."""

    def __init__(self, interval=0.08):
        self.window = None
        self.interval = interval
        self.latest = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def push(self, state):
        with self.lock:
            self.latest = state
        self.wake.set()

    def _run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            with self.lock:
                state, self.latest = self.latest, None
            if state is not None and self.window is not None:
                try:
                    self.window.evaluate_js(f'window.__onState && window.__onState({json.dumps(state, ensure_ascii=False)})')
                except Exception:
                    pass
            threading.Event().wait(self.interval)


class Api:
    def __init__(self, controller: Controller):
        self._controller = controller
        self._window = None

    def _call(self, fn, *args):
        try:
            result = fn(*args)
            return {'ok': True, 'state': result if isinstance(result, dict) else self._controller.snapshot()}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'state': self._controller.snapshot()}

    def get_state(self):
        return self._controller.snapshot()

    def choose_folder(self):
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return {'ok': True, 'state': self._controller.snapshot()}
        return self._call(self._controller.accept_input, picked[0])

    def choose_archive(self):
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, file_types=('ZIP / RAR 压缩包 (*.zip;*.rar)', '所有文件 (*.*)'))
        if not picked:
            return {'ok': True, 'state': self._controller.snapshot()}
        return self._call(self._controller.accept_input, picked[0])

    def accept_input(self, path):
        return self._call(self._controller.accept_input, path)

    def set_options(self, overwrite=None, auto=None):
        return self._call(self._controller.set_options, overwrite, auto)

    def scan(self):
        return self._call(self._controller.scan)

    def start(self):
        return self._call(self._controller.start)

    def resume(self):
        return self._call(self._controller.resume)

    def stop(self):
        return self._call(self._controller.stop)

    def select(self, source, checked):
        return self._call(self._controller.select, source, checked)

    def select_all(self, checked):
        return self._call(self._controller.select_all, checked)

    def cleanup(self):
        return self._call(self._controller.cleanup)

    def reset(self):
        return self._call(self._controller.reset)

    def dismiss(self):
        return self._call(self._controller.dismiss)

    def open_output(self):
        return self._call(self._controller.open_output)

    def open_job_dir(self):
        return self._call(self._controller.open_job_dir)


def _bind_drop(window, api):
    def on_drop(event):
        files = (event.get('dataTransfer') or {}).get('files') or []
        paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
        if len(paths) != 1:
            message = '每次请拖入一个文件夹或压缩包。' if paths or files else '没有读取到拖入的路径，请改用“选择文件夹 / 选择压缩包”。'
            window.evaluate_js(f'window.__onDropError({json.dumps(message, ensure_ascii=False)})')
            return
        result = api.accept_input(paths[0])
        if not result['ok']:
            window.evaluate_js(f'window.__onDropError({json.dumps(result["error"], ensure_ascii=False)})')
    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)


def main(debug=False):
    pusher = StatePusher()
    controller = Controller(notify=pusher.push)
    api = Api(controller)
    window = webview.create_window(
        'RY 文档归档助手', str(WEB_DIR / 'index.html'), js_api=api,
        width=1200, height=800, min_size=(980, 680), background_color='#FFFFFF')
    api._window = window
    pusher.window = window

    def on_closing():
        if controller.busy:
            window.evaluate_js("window.__onCloseBlocked && window.__onCloseBlocked()")
            return False
        return True

    window.events.closing += on_closing
    webview.start(_bind_drop, (window, api), gui='edgechromium', debug=debug, private_mode=True)


if __name__ == '__main__':
    main(debug=True)
