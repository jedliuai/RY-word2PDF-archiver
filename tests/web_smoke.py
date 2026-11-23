"""Explicit smoke test for the WebView2 front end (not part of unittest discovery).

Opens the real pywebview window, waits for the page + JS bridge, feeds a temporary folder
through the same Api the UI uses, checks the rendered screen, then closes. Runs no WPS/绿盾.
Usage: python tests/web_smoke.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402
import web_app  # noqa: E402
from app_controller import Controller  # noqa: E402

results = {}


def run(window, api, folder):
    try:
        for _ in range(100):
            if window.evaluate_js('!!(window.pywebview && window.pywebview.api && document.querySelector("h1"))'):
                break
            time.sleep(0.1)
        results['start_title'] = window.evaluate_js('document.querySelector("h1").textContent')
        results['bridge'] = window.evaluate_js('typeof window.pywebview.api.start')
        outcome = api.accept_input(str(folder))
        results['accept_ok'] = outcome['ok']
        time.sleep(0.6)
        results['card_path'] = window.evaluate_js('(document.querySelector(".card .primary-text")||{}).textContent')
        results['start_enabled'] = window.evaluate_js('!document.querySelector("[data-action=start]").disabled')
        api.set_options(None, True)
        time.sleep(0.4)
        results['auto_switch'] = window.evaluate_js('document.querySelector("[data-key=auto]").getAttribute("aria-checked")')
        results['steps'] = window.evaluate_js('document.querySelectorAll("#steps li").length')
    except Exception as exc:  # report instead of hanging the window
        results['exception'] = repr(exc)
    finally:
        window.destroy()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / '样例 资料'
        folder.mkdir()
        pusher = web_app.StatePusher()
        controller = Controller(notify=pusher.push)
        api = web_app.Api(controller)
        window = webview.create_window('smoke', str(web_app.WEB_DIR / 'index.html'), js_api=api, width=1100, height=760)
        api._window = window
        pusher.window = window
        webview.start(run, (window, api, folder), gui='edgechromium', private_mode=True)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        expected = {'start_title': '选择要归档的资料', 'bridge': 'function', 'accept_ok': True,
                    'card_path': str(folder), 'start_enabled': True, 'auto_switch': 'true', 'steps': 5}
        failures = {k: results.get(k) for k, v in expected.items() if results.get(k) != v}
        print('SMOKE OK' if not failures else f'SMOKE FAILED: {failures}')
        return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
