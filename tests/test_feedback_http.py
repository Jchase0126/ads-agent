"""崩溃反馈的 HTTP 接口（真实 HTTP，不需要 ADS）。

为什么单独测：

* backend/feedback.py 已经有单元测试（tests/test_feedback.py），但它测的是
  "报告怎么拼"；**面板真正会打的是这几个端点**，接错线（路径、鉴权、
  字段名、截断）单元测试一个都发现不了。
* 这里要守住的三条纪律：
  1. 新端点必须和别的一样走统一鉴权（未带令牌 → 401，不能漏）；
  2. 处理函数 never-raise（坏输入 → 4xx/500 JSON，绝不是空响应或断连）；
  3. 看门狗判卡死靠的是**心跳文件**，这里用真文件把"新鲜/陈旧"
     两种状态都造出来，而不是 mock 掉判定逻辑。

纪律：临时配置/数据目录（ADS_AGENT_DATA_DIR，绝不碰真实 %LOCALAPPDATA%）、
随机端口、后台线程异常必须判失败。

运行::

    python tests/test_feedback_http.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, run  # noqa: E402

BACKEND = add_path("backend")

TOKEN = "feedback-http-token-" + "Zk3P" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_feedback_http_")
_CFG = os.path.join(_TMP, "config.ini")
_DATA = os.path.join(_TMP, "data")

with open(_CFG, "w", encoding="utf-8") as f:
    f.write(
        "[llm]\n"
        "base_url = http://127.0.0.1:1\n"
        "model = test-model\n"
        "api_key = test-key-not-a-real-secret\n"
        "\n[ads]\n"
        "host = 127.0.0.1\n"
        "port = 8762\n"
        f"token = {TOKEN}\n"
        "\n[agent]\n"
        "sim_timeout = 120\n"
        "\n[feedback]\n"
        "enabled = true\n"
        "auto_open_browser = false\n"
        "include_logs = true\n"
        "max_log_lines = 20\n"
        "hang_seconds = 180\n"
    )

# 必须在 import backend 之前设好：paths.data_root() 就是在导入/调用时读它。
os.environ["ADS_AGENT_CONFIG"] = _CFG
os.environ["ADS_AGENT_DATA_DIR"] = _DATA
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    os.environ[_k] = ("," + _existing) if _existing else "127.0.0.1,localhost"

import adslog  # noqa: E402

adslog.LOG_DIR = _TMP
adslog.LOG_FILE = os.path.join(_TMP, "backend.log")
adslog.setup(echo_console=False)

import feedback  # noqa: E402
import server  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_httpd = None
_HOST = "127.0.0.1"
_PORT = 0


def _ensure_server():
    global _httpd, _PORT
    import config as config_mod
    server.CFG.update(config_mod.load())
    if _httpd is None:
        _httpd = ThreadingHTTPServer((_HOST, 0), server.Handler)
        _httpd.daemon_threads = True
        _PORT = _httpd.server_address[1]
        threading.Thread(target=_httpd.serve_forever, daemon=True,
                         name="feedback-http-backend").start()


def _call(method, path, body=None, token=True, timeout=30):
    """发一次请求，返回 (status, json)。4xx/5xx 也照常返回，不当异常。"""
    _ensure_server()
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Ads-Agent-Token"] = server.CFG["ads_token"]
    req = urllib.request.Request(f"http://{_HOST}:{_PORT}{path}",
                                 data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"raw": raw}


def _heartbeat_file():
    return feedback._heartbeat_path(feedback.HEARTBEAT_NAME)


def _write_fresh_heartbeat(pid=None):
    """造一份"刚写过"的 ADS 端心跳。pid 默认用本进程（必然存活）。"""
    path = _heartbeat_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": feedback.HEARTBEAT_NAME, "at": time.time(),
                   "pid": os.getpid() if pid is None else pid,
                   "payload": {"state": "idle"}}, f)


def _age_heartbeat(seconds=3600, pid=None):
    """把心跳的 at 改成很久以前，模拟"对面主线程卡住、心跳停摆"。"""
    path = _heartbeat_file()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["at"] = time.time() - seconds
    if pid is not None:
        data["pid"] = pid
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _clear_heartbeat():
    try:
        os.remove(_heartbeat_file())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 0. 数据根必须落在临时目录（否则会往真实用户目录里写报告）
# ---------------------------------------------------------------------------

def test_data_root_is_isolated_to_temp():
    root = os.path.normcase(os.path.abspath(feedback.feedback_dir(create=False)))
    tmp = os.path.normcase(os.path.abspath(_TMP))
    ok(root.startswith(tmp), f"反馈目录必须在临时数据根下，实际={root}")
    eq(os.environ.get("ADS_AGENT_DATA_DIR"), _DATA, "测试不得覆盖 ADS_AGENT_DATA_DIR")


# ---------------------------------------------------------------------------
# 1. POST /feedback/report：正常返回 url + markdown
# ---------------------------------------------------------------------------

def test_report_returns_url_and_markdown():
    status, data = _call("POST", "/feedback/report", {
        "kind": "crash",
        "error": "Traceback (most recent call last):\n"
                 '  File "C:\\proj\\ads_agent\\backend\\agent.py", line 42, in run\n'
                 "ZeroDivisionError: division by zero",
        "context": {"design": "MyAmp", "cell": "amp1", "note": "从面板点出来的"},
    })
    eq(status, 200, f"正常上报应当 200，实际 {status}: {data}")
    eq(data.get("ok"), True, "上报成功应回 ok=true")
    markdown = data.get("markdown") or ""
    ok(markdown, "必须返回 markdown（面板要显示/复制）")
    contains(markdown, "ZeroDivisionError", "markdown 里应包含原始报错")
    url = data.get("url") or ""
    ok(url.startswith("https://github.com/"), f"必须给出 issue 预填地址，实际={url!r}")
    contains(url, "title=", "issue 地址应带预填标题")
    ok(data.get("title"), "必须返回 issue 标题")
    ok(data.get("fingerprint"), "必须返回去重指纹")
    ok(os.path.isdir(data.get("dir") or ""), "报告目录应当真实存在")
    paths = data.get("paths") or {}
    ok(paths.get("json") or paths.get("md"),
       f"首次上报应当落盘（除非刚上报过同一个 bug）：{paths}")
    # 隐私：设计名/单元名不该出现在报告正文里
    ok("MyAmp" not in markdown, "设计名必须被脱敏")

    status2, listed = _call("GET", "/feedback/list?limit=5")
    eq(status2, 200, "列表接口应当 200")
    ok(listed.get("reports"), "落盘之后列表里应当看得见")


# ---------------------------------------------------------------------------
# 2. 未鉴权 → 401（新端点不能绕过统一鉴权）
# ---------------------------------------------------------------------------

def test_endpoints_require_token():
    for method, path, body in (
        ("GET", "/feedback/pending", None),
        ("GET", "/feedback/list", None),
        ("POST", "/feedback/report", {"kind": "crash", "error": "boom"}),
    ):
        status, data = _call(method, path, body, token=False)
        eq(status, 401, f"{method} {path} 未带令牌必须 401，实际 {status}: {data}")
        eq(data.get("error"), "unauthorized", "401 响应体约定")


# ---------------------------------------------------------------------------
# 3. 超长 error：截断而不是拒绝（也不能把服务打挂）
# ---------------------------------------------------------------------------

def test_oversized_error_is_truncated():
    huge = "X" * 30000 + "\nRuntimeError: 尾巴还在"
    status, data = _call("POST", "/feedback/report",
                         {"kind": "error", "error": huge})
    eq(status, 200, f"超长 error 应被截断后照常返回，实际 {status}: {data}")
    eq(data.get("truncated"), True, "截断时必须在响应里如实说明")
    eq(data.get("max_chars"), server.REPORT_MAX_CHARS, "应回报截断上限")
    err = (data.get("report") or {}).get("error") or ""
    ok(len(err) <= server.REPORT_MAX_CHARS,
       f"落进报告的正文不得超过上限，实际 {len(err)}")
    ok(err, "截断后仍应有正文")


def test_report_rejects_unknown_kind_and_bad_context():
    status, data = _call("POST", "/feedback/report",
                         {"kind": "explosion", "error": "x"})
    eq(status, 400, f"未知 kind 必须 400，实际 {status}: {data}")
    ok(data.get("error"), "400 必须带中文原因")

    status, data = _call("POST", "/feedback/report",
                         {"kind": "crash", "error": "x", "context": "not-a-dict"})
    eq(status, 400, f"context 非对象必须 400，实际 {status}: {data}")

    status, data = _call("POST", "/feedback/report", {"kind": "crash", "error": "  "})
    eq(status, 400, f"空 error 必须 400，实际 {status}: {data}")


# ---------------------------------------------------------------------------
# 4. GET /feedback/pending：心跳新鲜 stale=false；心跳陈旧 stale=true
# ---------------------------------------------------------------------------

def test_pending_fresh_heartbeat_is_not_stale():
    _write_fresh_heartbeat()
    try:
        status, data = _call("GET", "/feedback/pending")
        eq(status, 200, f"pending 应当 200，实际 {status}: {data}")
        eq(data.get("ok"), True, "pending 应回 ok=true")
        ok("duplicates" in data, "pending 必须带 duplicates（重复指纹）")
        hang = data.get("hang") or {}
        eq(hang.get("heartbeat"), feedback.HEARTBEAT_NAME,
           "判定的必须是面板那份心跳的名字")
        eq(hang.get("stale"), False, "刚写过心跳不该判卡死")
        eq(hang.get("auto_reported"), None, "没卡死就不该自动上报")
        ok((hang.get("age") or 0) < 30, f"跳龄应接近 0，实际 {hang.get('age')}")
    finally:
        _clear_heartbeat()


def test_pending_stale_heartbeat_is_detected():
    _write_fresh_heartbeat(pid=os.getpid())  # 本进程存活 → 只测"陈旧"，不触发自动上报
    try:
        _age_heartbeat(3600)
        status, data = _call("GET", "/feedback/pending")
        eq(status, 200, f"pending 应当 200，实际 {status}: {data}")
        hang = data.get("hang") or {}
        eq(hang.get("stale"), True, "一小时没心跳必须判卡死")
        ok((hang.get("age") or 0) > 3000, f"跳龄应反映真实间隔，实际 {hang.get('age')}")
        eq(hang.get("alive"), True, "进程还活着时 alive 应为 true")
        eq(hang.get("auto_reported"), None,
           "进程还活着（可能只是忙）不该自动上报")
    finally:
        _clear_heartbeat()


# ---------------------------------------------------------------------------
# 5. 卡死 + 进程已消失 → 自动生成报告，且重复查询不会刷出第二份
# ---------------------------------------------------------------------------

def test_stale_and_dead_pid_auto_reports_once():
    _write_fresh_heartbeat(pid=os.getpid())
    _age_heartbeat(3600, pid=4_000_004)  # 这个 pid 几乎必然不存在
    try:
        status, data = _call("GET", "/feedback/pending")
        eq(status, 200, f"pending 应当 200，实际 {status}: {data}")
        hang = data.get("hang") or {}
        eq(hang.get("stale"), True, "心跳陈旧必须判卡死")
        eq(hang.get("alive"), False, "不存在的 pid 应判为已退出")
        reported = hang.get("auto_reported") or {}
        ok(reported, f"卡死且进程已退出必须自动生成报告：{hang}")
        eq(reported.get("duplicate"), False, "第一次自动上报不该是重复")
        ok((reported.get("paths") or {}).get("json"),
           f"自动上报应真的落盘：{reported}")

        # 再查一次：靠 feedback.record 的内置指纹去重，不得刷出第二份
        status, again = _call("GET", "/feedback/pending")
        eq(status, 200, "第二次查询仍应 200")
        second = (again.get("hang") or {}).get("auto_reported") or {}
        eq(second.get("duplicate"), True,
           f"同一处卡死在窗口内应判重复，实际：{second}")
        eq(second.get("paths") or {}, {}, "重复时不该再落盘")
    finally:
        _clear_heartbeat()


# ---------------------------------------------------------------------------
# 6. GET /feedback/list：返回目录与清单
# ---------------------------------------------------------------------------

def test_list_returns_dir_and_reports():
    status, data = _call("GET", "/feedback/list?limit=10")
    eq(status, 200, f"list 应当 200，实际 {status}: {data}")
    eq(data.get("ok"), True, "list 应回 ok=true")
    ok(data.get("dir"), "list 必须返回反馈目录（用户要能找到文件）")
    ok(os.path.isdir(data.get("dir")), "返回的目录必须真实存在")
    eq(data.get("limit"), 10, "limit 应回显")
    ok(isinstance(data.get("reports"), list), "reports 必须是列表")
    ok(data.get("reports"), "前面的用例已经落过盘，这里不该是空列表")
    first = data["reports"][0]
    ok(first.get("name") and first.get("path"), f"每条记录要有文件名与路径：{first}")
    ok("hooks" in data, "list 应带上钩子安装状态（诊断用）")
    ok(data.get("privacy_notice"), "list 应带上隐私说明文案")
    ne(data.get("repo"), None, "list 应带上反馈仓库名")

    # limit 越界要被夹住，而不是报错
    status, small = _call("GET", "/feedback/list?limit=0")
    eq(status, 200, "非法 limit 不应报错")
    eq(small.get("limit"), 1, "limit 下限应夹到 1")


# ---------------------------------------------------------------------------
# 7. 配置：/config 能看到反馈开关，POST /config/feedback 能改回来
# ---------------------------------------------------------------------------

def test_config_exposes_and_updates_feedback_keys():
    status, data = _call("GET", "/config")
    eq(status, 200, "/config 应当 200")
    eq(data.get("feedback_enabled"), True, "默认应开启反馈")
    eq(data.get("feedback_hang_seconds"), 180, "默认卡死阈值 180s")
    eq(data.get("feedback_max_log_lines"), 20, "应读到临时 config 里的 20")

    status, saved = _call("POST", "/config/feedback",
                          {"feedback_hang_seconds": 240,
                           "feedback_auto_open_browser": True})
    eq(status, 200, f"保存反馈配置应当 200，实际 {status}: {saved}")
    eq(saved.get("ok"), True, "保存成功应回 ok=true")
    eq(saved.get("feedback_hang_seconds"), 240, "返回值应是写入后的实际值")
    eq(saved.get("feedback_auto_open_browser"), True, "布尔开关应写入")
    eq(server.CFG.get("feedback_hang_seconds"), 240, "内存 CFG 应立即生效")

    # 只传一个键时，其它键不得被重置
    status, saved = _call("POST", "/config/feedback", {"feedback_hang_seconds": 180})
    eq(status, 200, "再次保存应当 200")
    eq(saved.get("feedback_auto_open_browser"), True,
       "没传的键必须保持原值（否则面板保存会静默重置开关）")

    # 非法值：400，且不能把配置写坏
    status, bad = _call("POST", "/config/feedback", {"feedback_hang_seconds": -5})
    eq(status, 400, f"负数阈值必须 400，实际 {status}: {bad}")
    eq(server.CFG.get("feedback_hang_seconds"), 180, "非法值不得改动配置")

    # 老接口 /config 也必须能写这几个键（任务书要求），且**只在带了才写**
    status, merged = _call("POST", "/config", {"feedback_max_log_lines": 30})
    eq(status, 200, f"/config 也应能写反馈键，实际 {status}: {merged}")
    eq(merged.get("feedback_max_log_lines"), 30, "/config 应返回写入后的值")
    eq(merged.get("feedback_auto_open_browser"), True,
       "/config 里没传的反馈键必须保持原值（面板保存模型时从不带这些字段）")
    # 面板真实的保存请求（完全没有 feedback_* 字段）不得动反馈配置
    status, plain = _call("POST", "/config", {"model": "test-model"})
    eq(status, 200, f"面板式保存应当 200，实际 {status}: {plain}")
    eq(server.CFG.get("feedback_max_log_lines"), 30,
       "不带 feedback_* 的保存不得重置反馈配置")


# ---------------------------------------------------------------------------
# 8. 看门狗线程本身：能起来、能写心跳、重复调用不叠加
#
# 这条是回归测试：start_feedback_watch() 里曾经漏了 `global _FEEDBACK_THREAD`，
# 导致**第一次调用就 UnboundLocalError** —— 只有真跑 main() 才会踩到，
# HTTP 接口测试一个都发现不了（服务是测试自己起的）。所以这里直接把
# 启动函数调一遍。
# ---------------------------------------------------------------------------

def test_start_feedback_watch_is_idempotent_and_writes_heartbeat():
    _clear_heartbeat()
    first = server.start_feedback_watch()
    ok(first is True, "第一次启动看门狗应当返回 True")
    eq(server.start_feedback_watch(), False, "重复启动应当返回 False（不得叠加线程）")
    thread = server._FEEDBACK_THREAD
    ok(thread is not None and thread.is_alive(), "看门狗线程必须真的在跑")
    ok(thread.daemon, "看门狗必须是 daemon（不能拖住进程退出）")

    # 线程首轮要等 30s 才写心跳，这里直接驱动同一个入口，验证它写得出来
    ok(feedback.write_heartbeat(server._BACKEND_HEARTBEAT, server._feedback_payload()),
       "后端心跳应能写入")
    beat = feedback.read_heartbeat(server._BACKEND_HEARTBEAT)
    eq(beat.get("name"), server._BACKEND_HEARTBEAT, "后端心跳用自己的名字")
    ne(beat.get("name"), feedback.HEARTBEAT_NAME,
       "后端心跳不得覆盖面板心跳（否则卡死判定永远不成立）")
    payload = beat.get("payload") or {}
    ok("active_turns" in payload or "compute" in payload,
       f"心跳应带上后端正在干什么：{payload}")


# ---------------------------------------------------------------------------

def teardown():
    global _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            _httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        _httpd = None
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run(globals(), "崩溃反馈 HTTP 接口")
    finally:
        teardown()
    sys.exit(code)
