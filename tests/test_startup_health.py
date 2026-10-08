"""Real Qt timer and HTTP startup paths, isolated from ADS and user data."""
import ast
import json
import os
from pathlib import Path
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, run

ADDON = Path(add_path('addon', 'ads_agent'))
add_path('backend')
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import instance
import paths
import backend_launcher
import toolserver
import qtcompat

APP = qtcompat.QtWidgets().QApplication.instance() or qtcompat.QtWidgets().QApplication([])


def test_launcher_and_probe_use_exactly_one_health_path():
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            hits.append(self.path)
            payload = dict(status='ok', service=instance.SERVICE_BACKEND,
                           protocol=paths.PROTOCOL_VERSION, identity=instance.identity())
            body = json.dumps(payload).encode()
            self.send_response(200 if self.path == '/health' else 401)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{httpd.server_port}'
    try:
        for suffix in ('', '/', '/health', '/health/'):
            result = instance.probe(url + suffix)
            ok(instance.evaluate(result, 'backend')['usable'], result)
        with patch.object(backend_launcher, '_base_url', return_value=url), \
                patch.object(backend_launcher, '_instance', return_value=instance), \
                patch.object(backend_launcher.subprocess, 'Popen') as spawn:
            ok(backend_launcher.backend_alive())
            ok(backend_launcher.ensure_backend(wait=.1)[0])
            spawn.assert_not_called()
        eq(hits, ['/health'] * 6)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_real_qt_toolserver_starts_reuses_and_releases_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    cfg = Path(paths.config_path())
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'[ads]\nhost = 127.0.0.1\nport = {port}\n', encoding='utf-8')
    timer = None
    try:
        for _ in range(2):
            url = toolserver.ensure_started()
            timer = toolserver._pump_timer
            ok(isinstance(timer, qtcompat.QtCore().QTimer))
            ok(timer.isActive())
            APP.processEvents()
            verdict = instance.evaluate(instance.probe(url), 'toolserver')
            ok(verdict['usable'], verdict)
            eq(toolserver.ensure_started(), url)
            ok(toolserver._pump_timer is timer)
            toolserver.shutdown()
            ok(not timer.isActive())
            ok(toolserver._pump_timer is None)
            with socket.socket() as released:
                released.bind(('127.0.0.1', port))
    finally:
        toolserver.shutdown()
        APP.processEvents()


def test_accessors_are_not_imported_as_qt_modules():
    for source in ADDON.glob('*.py'):
        tree = ast.parse(source.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'qtcompat':
                ok(not any(alias.name in ('QtCore', 'QtGui', 'QtWidgets') for alias in node.names),
                   f'{source.name}:{node.lineno}: Qt accessors require a call')


if __name__ == '__main__':
    raise SystemExit(run(globals(), 'Real Qt startup and health integration'))
