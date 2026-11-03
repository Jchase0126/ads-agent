"""Offline update security and HTTP contract regression tests."""
import hashlib
import http.client
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
import update_service as u


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


def archive(extra=None):
    out = io.BytesIO()
    required = ("install_addon.py", "install_addon.bat", "release_manifest.py",
                "backend/paths.py", "backend/server.py", "backend/update_service.py",
                "addon/ads_agent/__init__.py", "addon/ads_agent/panel.py",
                "addon/ads_agent/toolserver.py", "addon/ads_agent/ads_ops.py")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in required:
            z.writestr("ADSAgent-1.4.0/" + name, "contents")
        if extra:
            for name, value in extra:
                z.writestr(name, value)
    return out.getvalue()


def metadata(data, **changes):
    asset = {"name": "ADSAgent-1.4.0.zip", "state": "uploaded", "size": len(data),
             "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
             "browser_download_url": "https://github.com/Jchase0126/ads-agent/releases/download/v1.4.0/ADSAgent-1.4.0.zip"}
    payload = {"tag_name": "v1.4.0", "draft": False, "prerelease": False, "assets": [asset]}
    payload.update(changes)
    return payload


def fake_open(payload, data):
    def inner(url, hosts, accept):
        if url == u.API_URL:
            return Response(json.dumps(payload).encode())
        return Response(data, {"Content-Length": str(len(data))})
    return inner


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Save original first: tempfile is a shared module.
        self.original_mkdtemp = tempfile.mkdtemp
        self.mk = patch.object(u.tempfile, "mkdtemp", side_effect=lambda prefix: self.original_mkdtemp(prefix=prefix, dir=self.tmp.name))
        self.mk.start()
        self.version = patch.object(u.paths, "PLUGIN_VERSION", "1.3.2")
        self.version.start()

    def tearDown(self):
        self.version.stop()
        self.mk.stop()
        self.tmp.cleanup()

    def run_prepare(self, payload, data):
        with patch.object(u, "_open", side_effect=fake_open(payload, data)):
            return u.prepare()

    def test_success_verified_and_isolated_no_install(self):
        data = archive()
        with patch.object(u, "_open", side_effect=fake_open(metadata(data), data)):
            self.assertEqual(u.check()["status"], "update_available")
            result = u.prepare()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(os.path.isfile(result["installer_path"]))
        with open(result["package_path"], "rb") as stream:
            self.assertEqual(stream.read(), data)
        self.assertIn("尚未安装", result["message"])
        self.assertIn("关闭所有 ADS", result["instruction"])
        self.assertIn("不是事务", result["instruction"])

    def test_downgrade_and_equal_need_no_asset(self):
        for tag in ("v1.3.0", "1.3.2"):
            with self.subTest(tag=tag), patch.object(u, "_open", side_effect=fake_open(metadata(b"", tag_name=tag, assets=[]), b"")):
                self.assertEqual(u.check()["status"], "up_to_date")
                self.assertEqual(u.prepare()["status"], "up_to_date")
                self.assertEqual(os.listdir(self.tmp.name), [])

    def test_new_version_no_assets_bad_digest_ambiguous_or_unstable(self):
        data = archive()
        base = metadata(data)
        for payload in (metadata(data, assets=[]), metadata(data, assets=[dict(base['assets'][0], digest=None)]),
                        metadata(data, assets=base['assets'] * 2), metadata(data, prerelease=True),
                        metadata(data, draft=True), metadata(data, tag_name='v1.4.0-rc1')):
            with self.subTest(payload=payload), patch.object(u, "_open", side_effect=fake_open(payload, data)):
                self.assertEqual(u.check()["status"], "error")
                self.assertEqual(u.prepare()["status"], "error")
                self.assertEqual(os.listdir(self.tmp.name), [])

    def test_wrong_digest_size_and_network_cleanup(self):
        data = archive()
        base = metadata(data)
        for asset in (dict(base['assets'][0], digest='sha256:' + '0'*64),
                      dict(base['assets'][0], size=len(data)+1),
                      dict(base['assets'][0], size=u.MAX_ZIP+1)):
            self.assertEqual(self.run_prepare(metadata(data, assets=[asset]), data)['status'], 'error')
            self.assertEqual(os.listdir(self.tmp.name), [])
        with patch.object(u, '_open', side_effect=OSError('offline')):
            self.assertIn('offline', u.check()['message'])
            self.assertEqual(u.prepare()['status'], 'error')

    def test_asset_url_and_redirect_allowlist(self):
        data = archive()
        asset = metadata(data)['assets'][0]
        for url in ('http://github.com/a', 'https://github.com.evil.test/a',
                    'https://user@github.com/a', 'https://github.com:444/a',
                    'https://github.com/Jchase0126/other/releases/download/v1.4.0/ADSAgent-1.4.0.zip'):
            self.assertEqual(self.run_prepare(metadata(data, assets=[dict(asset, browser_download_url=url)]), data)['status'], 'error')
        handler = u._Redirect(u._ALLOWED_DOWNLOAD_HOSTS)
        req = urllib.request.Request('https://github.com/x')
        for url in ('http://release-assets.githubusercontent.com/x', 'https://evil.test/x', 'https://release-assets.githubusercontent.com.evil.test/x'):
            with self.assertRaises(u.UpdateError):
                handler.redirect_request(req, None, 302, '', {}, url)
        self.assertIsNotNone(u._Redirect(u._ALLOWED_DOWNLOAD_HOSTS).redirect_request(req, None, 302, '', {}, 'https://release-assets.githubusercontent.com/x'))
        handler = u._Redirect(u._ALLOWED_DOWNLOAD_HOSTS)
        for _ in range(4):
            handler.redirect_request(req, None, 302, '', {}, 'https://github.com/x')
        with self.assertRaises(u.UpdateError):
            handler.redirect_request(req, None, 302, '', {}, 'https://github.com/x')

    def test_zip_traversal_symlink_duplicate_and_limits(self):
        link = zipfile.ZipInfo('ADSAgent-1.4.0/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        backslash = zipfile.ZipInfo('placeholder')
        backslash.filename = 'ADSAgent-1.4.0/a\\b'
        for item in (('../escape', 'x'), ('ADSAgent-1.4.0/../escape', 'x'),
                     ('ADSAgent-1.4.0/C:evil', 'x'), (backslash, 'x'),
                     ('ADSAgent-1.4.0/CON.txt', 'x'), ('ADSAgent-1.4.0/INSTALL_ADDON.PY', 'x'),
                     (link, 'target')):
            data = archive([item])
            with self.subTest(item=item):
                self.assertEqual(self.run_prepare(metadata(data), data)['status'], 'error')
                self.assertEqual(os.listdir(self.tmp.name), [])
        data = archive()
        with patch.object(u, 'MAX_EXPANDED', 1):
            self.assertEqual(self.run_prepare(metadata(data), data)['status'], 'error')
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_bad_zip_cleans_up(self):
        data = b'not a zip'
        self.assertEqual(self.run_prepare(metadata(data), data)['status'], 'error')
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_preflight_rejects_huge_central_directory(self):
        # EOCD 声称有远超 MAX_FILES 的目录项：必须在 ZipFile 解析前拒绝。
        body = b'PK\x03\x04placeholder'
        eocd = b'PK\x05\x06' + b'\x00' * 4 + struct.pack('<HHIIIHH', 0, 0, 0,
                                                        u.MAX_FILES + 1, 10, 0, 0)
        bad = body + eocd
        self.assertEqual(self.run_prepare(metadata(bad), bad)['status'], 'error')
        self.assertEqual(os.listdir(self.tmp.name), [])
        # 正常包不被误伤。
        good = archive()
        self.assertEqual(self.run_prepare(metadata(good), good)['status'], 'ready')
        # 单飞托管：成功目录保留，但只有一份，下次准备前会清掉旧的。
        dirs = [d for d in os.listdir(self.tmp.name)]
        self.assertEqual(len(dirs), 1)
        self.assertEqual(self.run_prepare(metadata(good), good)['status'], 'ready')
        self.assertEqual(len(os.listdir(self.tmp.name)), 1)

    def test_prepare_is_single_flight(self):
        gate = threading.Event()
        release_flag = threading.Event()
        data = archive()

        def slow_open(url, hosts, accept):
            if url == u.API_URL:
                return Response(json.dumps(metadata(data)).encode())
            gate.wait(2)
            release_flag.set()
            return Response(data, {"Content-Length": str(len(data))})

        with patch.object(u, "_open", side_effect=slow_open):
            first = threading.Thread(target=u.prepare)
            first.start()
            gate.set()
            release_flag.wait(2)  # 第一个 prepare 已拿到锁并进入下载
            second = u.prepare()
            first.join(5)
        self.assertEqual(second["status"], "error")
        self.assertIn("一次更新准备", second["message"])


class HttpTests(unittest.TestCase):
    def test_routes_require_auth_and_contract(self):
        import adslog
        import config
        from unittest.mock import MagicMock
        with patch.object(adslog, 'setup', return_value='test.log'), \
             patch.object(adslog, 'get', return_value=MagicMock()), \
             patch.object(config, 'load', return_value={**config.DEFAULTS, 'ads_token': 'test'}):
            import server
        with patch.object(server.ads_auth, 'check_token', side_effect=lambda token: token == 'valid'), \
             patch.object(u, 'check', return_value={'status': 'up_to_date'}) as check, \
             patch.object(u, 'prepare', return_value={'status': 'ready'}) as prepare:
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                def call(method, path, token=None, body=None):
                    headers = {'X-Ads-Agent-Token': token} if token else {}
                    conn.request(method, path, body=body, headers=headers)
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                self.assertEqual(call('GET', '/updates/check')[0], 401)
                self.assertEqual(call('POST', '/updates/prepare', body='{}')[0], 401)
                check.assert_not_called()
                prepare.assert_not_called()
                self.assertEqual(call('GET', '/updates/check', 'valid')[1]['status'], 'up_to_date')
                self.assertEqual(call('POST', '/updates/prepare', 'valid', '{"url":"evil"}')[0], 400)
                self.assertEqual(call('POST', '/updates/prepare', 'valid', '{')[0], 400)
                self.assertEqual(call('POST', '/updates/prepare', 'valid', '{}')[1]['status'], 'ready')
                prepare.assert_called_once()
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
