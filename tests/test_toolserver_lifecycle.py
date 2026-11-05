"""Listener lifecycle regressions, independent of Qt and ADS."""
import os
import queue
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path
add_path('addon', 'ads_agent')
import toolserver


class Timer:
    def stop(self):
        pass

    def deleteLater(self):
        pass


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(toolserver, '_pump_timer', Timer()),
            patch.object(toolserver, '_heartbeat_timer', None),
            patch.object(toolserver, 'ensure_heartbeat_timer'),
            patch.object(toolserver.authbridge, 'token'),
            patch.object(toolserver, '_setting', side_effect=lambda s, k, d: '0' if k == 'port' else d),
            patch.object(toolserver, '_register_instance'),
            patch.object(toolserver, '_clear_instance'),
            patch.object(toolserver, '_log'),
            patch.object(toolserver, '_SERVER_STOP_TIMEOUT', 1.5),
            patch.object(toolserver, '_jobs', queue.Queue()),
            patch.object(toolserver, '_active_jobs', {}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        try:
            toolserver.shutdown()
        finally:
            for p in reversed(self.patches):
                p.stop()

    def test_listener_port_is_exclusive_and_actual_address_is_returned(self):
        url = toolserver.ensure_started()
        port = toolserver._server.server_address[1]
        self.assertEqual(url, f'http://127.0.0.1:{port}')
        with socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                other.bind(('127.0.0.1', port))

    def test_changed_config_does_not_claim_to_listen_on_new_port(self):
        toolserver.ensure_started()
        old = toolserver._server
        with patch.object(toolserver, '_setting', side_effect=lambda s, k, d: '8763' if k == 'port' else d):
            with self.assertRaisesRegex(RuntimeError, '仍监听.*配置已改为'):
                toolserver.ensure_started()
        self.assertIs(toolserver._server, old)
        self.assertTrue(toolserver._server_thread.is_alive())

    def test_dead_listener_restarted_without_replaying_jobs(self):
        job = object()
        toolserver._jobs.put(job)
        toolserver._active_jobs[42] = {'name': 'non-idempotent'}
        toolserver.ensure_started()
        old, thread = toolserver._server, toolserver._server_thread
        self.assertTrue(toolserver._server_ready.wait(2))
        old.shutdown()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        toolserver.ensure_started()
        self.assertIsNot(toolserver._server, old)
        self.assertTrue(toolserver._server_thread.is_alive())
        self.assertEqual(old.socket.fileno(), -1)
        self.assertIs(toolserver._jobs.get_nowait(), job)
        self.assertTrue(toolserver._jobs.empty())
        self.assertEqual(toolserver._active_jobs[42]['name'], 'non-idempotent')
        current = toolserver._server
        toolserver.ensure_started()
        self.assertIs(toolserver._server, current)

    def test_shutdown_before_loop_preserves_live_server(self):
        gate = threading.Event()
        entered = threading.Event()
        class DelayedServer(ThreadingHTTPServer):
            def serve_forever(self, *args, **kwargs):
                entered.set()
                gate.wait(5)
                super().serve_forever(*args, **kwargs)
        try:
            with patch.object(toolserver, 'ThreadingHTTPServer', DelayedServer):
                toolserver.ensure_started()
                self.assertTrue(entered.wait(1))
                server = toolserver._server
                timer = toolserver._pump_timer
                with patch.object(toolserver, '_SERVER_STOP_TIMEOUT', .05):
                    with self.assertRaisesRegex(RuntimeError, '尚未进入'):
                        toolserver.shutdown()
                self.assertIs(toolserver._server, server)
                self.assertIs(toolserver._pump_timer, timer)
                self.assertNotEqual(server.socket.fileno(), -1)
                self.assertIsNone(toolserver._server_stop_thread)
                toolserver.ensure_started()
                self.assertIs(toolserver._server, server)
        finally:
            gate.set()

    def test_stop_timeout_keeps_server_and_rejects_restart(self):
        gate = threading.Event()
        class StuckServer(ThreadingHTTPServer):
            def service_actions(self):
                gate.wait(5)
        try:
            with patch.object(toolserver, 'ThreadingHTTPServer', StuckServer):
                toolserver.ensure_started()
                self.assertTrue(toolserver._server_ready.wait(2))
                server = toolserver._server
                with patch.object(toolserver, '_SERVER_STOP_TIMEOUT', .05):
                    with self.assertRaisesRegex(RuntimeError, '停止超时'):
                        toolserver.shutdown()
                self.assertIs(toolserver._server, server)
                self.assertNotEqual(server.socket.fileno(), -1)
                with self.assertRaisesRegex(RuntimeError, '正在停止'):
                    toolserver.ensure_started()
        finally:
            gate.set()

    def test_thread_start_failure_closes_listener(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('start failed')):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                toolserver.ensure_started()
        self.assertIsNone(toolserver._server)
        self.assertIsNone(toolserver._server_thread)
        toolserver._register_instance.assert_not_called()

    def test_dead_before_loop_shutdown_does_not_wait(self):
        class DeadServer(ThreadingHTTPServer):
            def serve_forever(self):
                return
        with patch.object(toolserver, 'ThreadingHTTPServer', DeadServer):
            toolserver.ensure_started()
            server, thread = toolserver._server, toolserver._server_thread
            thread.join(1)
            self.assertFalse(thread.is_alive())
            with patch.object(server, 'shutdown', side_effect=AssertionError('must not call')):
                toolserver.shutdown()
            self.assertEqual(server.socket.fileno(), -1)
            self.assertIsNone(toolserver._server)


if __name__ == '__main__':
    unittest.main()
