"""删除供应商（provider_remove）的测试（不需要 ADS，纯标准库）。

覆盖模型设置页「删除供应商」的后端语义：

* provider_remove=True 会删除 provider_model 所属供应商（同一连接身份）
  的**全部**模型 profile，并从 models 列表移除；
* 活动模型在被删组内时切到剩余第一个模型；删空时保持显式空配置；
* legacy 顶层 base_url/api_key 键跟随新的活动模型，不残留已删供应商；
* 删除其它供应商时活动模型保持不变；
* provider_remove 指向不存在的供应商时报错且不改文件；
* 注释与其它段落原样保留。

运行::

    python tests/test_provider_remove.py
"""

import configparser
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ne, ok, raises, run  # noqa: E402

BACKEND = add_path("backend")

# 必须在 import config 之前：config.py 在 import 时就会算 CONFIG_PATH
_TMP = tempfile.mkdtemp(prefix="ads_agent_provider_remove_")
_CFG = os.path.join(_TMP, "config.ini")
os.environ["ADS_AGENT_CONFIG"] = _CFG

import config as config_mod  # noqa: E402

PROFILES = {
    "a1": {"base_url": "https://a.example/v1", "api_key": "sk-a",
           "provider_name": "供应商A", "enabled": True},
    "a2": {"base_url": "https://a.example/v1", "api_key": "sk-a",
           "provider_name": "供应商A", "enabled": True},
    "b1": {"base_url": "https://b.example/v1", "api_key": "sk-b",
           "provider_name": "供应商B", "enabled": True},
}

CONFIG_TEMPLATE = """; 顶部注释必须保留
[llm]
base_url = https://a.example/v1
model = a1
api_key = sk-a
models = a1, a2, b1
profiles = %s

[ads]
host = 127.0.0.1
port = 8761
; 其它段落必须保留
token = test-local-token-not-legacy
""" % json.dumps(PROFILES, ensure_ascii=False)


def _fresh(body: str = CONFIG_TEMPLATE) -> None:
    with open(_CFG, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)


def _raw() -> str:
    with open(_CFG, "r", encoding="utf-8") as f:
        return f.read()


def _get(section: str, key: str) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_CFG, encoding="utf-8")
    return parser.get(section, key)


def _profiles() -> dict:
    return json.loads(_get("llm", "profiles"))


def test_remove_deletes_whole_provider_group():
    _fresh()
    status = config_mod.update_llm_settings(provider_model="a1", provider_remove=True)
    profiles = _profiles()
    ok("a1" not in profiles and "a2" not in profiles, "同连接身份的模型应全部删除")
    ok("b1" in profiles, "其它供应商不应被误删")
    eq(_get("llm", "models"), "b1", "models 列表应移除被删模型")
    eq(_get("llm", "model"), "b1", "活动模型在被删组内时应切到剩余第一个模型")
    eq(_get("llm", "base_url"), "https://b.example/v1",
       "legacy 连接键应跟随新的活动模型")
    eq(_get("llm", "api_key"), "sk-b", "legacy 密钥应跟随新的活动模型")
    eq(status["model"], "b1")
    eq(status["models"], ["b1"], "状态里的 models 应反映删除后的列表")
    eq(len(status["provider_groups"]), 1, "供应商分组应只剩一组")
    eq(status["provider_groups"][0]["provider_name"], "供应商B")


def test_remove_environment_managed_model_is_rejected():
    _fresh()
    before = _raw()
    previous = os.environ.get('ADS_AGENT_MODEL')
    try:
        os.environ['ADS_AGENT_MODEL'] = 'a1'
        raises(ValueError, lambda: config_mod.update_llm_settings(
            provider_model='a1', provider_remove=True))
        eq(_raw(), before)
    finally:
        if previous is None:
            os.environ.pop('ADS_AGENT_MODEL', None)
        else:
            os.environ['ADS_AGENT_MODEL'] = previous


def test_remove_non_active_provider_keeps_active_model():
    _fresh()
    status = config_mod.update_llm_settings(provider_model="b1", provider_remove=True)
    profiles = _profiles()
    ok("b1" not in profiles, "被删供应商的 profile 应移除")
    eq(_get("llm", "model"), "a1", "删除非活动供应商不应改变活动模型")
    eq(_get("llm", "base_url"), "https://a.example/v1",
       "legacy 连接键应保持指向剩余的活动模型")
    eq(status["models"], ["a1", "a2"])


def test_remove_last_provider_stays_empty_and_can_add():
    body = """; 注释
[llm]
base_url = https://a.example/v1
model = a1
api_key = sk-a
models = a1
profiles = %s
""" % json.dumps({"a1": PROFILES["a1"]}, ensure_ascii=False)
    _fresh(body)
    status = config_mod.update_llm_settings(provider_model="a1", provider_remove=True)
    eq(status["deleted_models"], ["a1"])
    eq(_get("llm", "models"), "")
    eq(_get("llm", "model"), "")
    for _ in range(2):
        cfg = config_mod.load()
        eq(cfg["llm_model"], "")
        eq(cfg["llm_models"], [])
        eq(cfg["llm_profiles"], {})
        eq(config_mod.provider_groups(cfg), [])
    config_mod.update_llm_settings()  # 仅保存其他配置时不能创建空模型
    eq(config_mod.load()["llm_profiles"], {})
    added = config_mod.update_llm_settings(model="new", provider_models=["new"],
                                           base_url="https://new.example/v1", api_key="key")
    eq(added["model"], "new")
    eq(added["models"], ["new"])
    eq(list(config_mod.load()["llm_profiles"]), ["new"])


def test_legacy_config_still_inherits_connection():
    _fresh("[llm]\nmodel = old1\nmodels = old1, old2\nbase_url = https://old.example/v1\napi_key = old-key\n")
    cfg = config_mod.load()
    eq(cfg["llm_models"], ["old1", "old2"])
    eq(cfg["llm_profiles"]["old2"]["api_key"], "old-key")
    config_mod.update_llm_settings(provider_model="old1", provider_remove=True)
    eq(config_mod.load()["llm_profiles"], {})


def test_remove_unknown_provider_is_rejected_without_write():
    _fresh()
    before = _raw()
    err = raises(ValueError,
                 lambda: config_mod.update_llm_settings(provider_model="ghost",
                                                        provider_remove=True),
                 "删除不存在的供应商应报错")
    ok("不存在" in str(err) or "重新载入" in str(err), "报错信息应可读")
    eq(_raw(), before, "失败时不应改写配置文件")
    ne(_raw(), "")


def test_remove_preserves_comments_and_other_sections():
    _fresh()
    config_mod.update_llm_settings(provider_model="a1", provider_remove=True)
    raw = _raw()
    ok(raw.startswith("; 顶部注释必须保留"), "顶部注释应保留")
    ok("; 其它段落必须保留" in raw, "段内注释应保留")
    eq(_get("ads", "token"), "test-local-token-not-legacy", "其它段落不应被动")
    eq(_get("ads", "port"), "8761")


def test_provider_remove_requires_bool():
    _fresh()
    raises(ValueError,
           lambda: config_mod.update_llm_settings(provider_model="a1",
                                                  provider_remove="yes"),
           "provider_remove 非布尔值应报错")


def test_delete_endpoint_auth_receipt_and_status():
    import http.client
    import threading
    from unittest.mock import patch
    _fresh()
    import server
    server.CFG.update(config_mod.load())
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        def call(body, token=None, path="/config/provider/delete"):
            headers = {"X-Ads-Agent-Token": token} if token else {}
            conn.request("POST", path, body=json.dumps(body), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        with patch.object(server.ads_auth, "check_token", side_effect=lambda value: value == "valid"):
            eq(call({"provider_model": "a1"})[0], 401)
            eq(_profiles(), PROFILES)
            eq(call({}, "valid")[0], 400)
            eq(call({"provider_model": "ghost"}, "valid")[0], 400)
            code, result = call({"provider_model": "a1"}, "valid")
            eq(code, 200)
            eq(result["deleted"], True)
            eq(result["deleted_models"], ["a1", "a2"])
            eq(result["models"], ["b1"])
            ok("feedback_enabled" in result, "必须返回完整状态")
            code, result = call({"provider_model": "b1"}, "valid")
            eq(code, 200)
            eq(result["models"], [])
            eq(result["provider_groups"], [])
            eq(call({"provider_model": "b1"}, "valid")[0], 400)
            eq(call({}, "valid", "/old-version-unknown-route")[0], 404)
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


if __name__ == "__main__":
    sys.exit(run(globals(), "删除供应商 provider_remove"))
