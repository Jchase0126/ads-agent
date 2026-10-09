"""实例登记与身份校验：不以 /health 200 作为复用依据。

改造前"后端在不在"只看 /health 是否返回 200。这条捷径有四种失败：

* 端口上坐着**另一个 ADS Agent 安装**的后端 —— 令牌不属于这份安装，全部 401；
* 端口上坐着**旧版本**后端 —— 少了一些接口，界面报莫名其妙的错；
* 端口上坐着**完全无关的本地程序** —— 连得上但问不出东西；
* 同一份安装**起了两次** —— 两个进程各持一份内存配置，用户改的设置互相覆盖。

本测试用例逐条钉住这四种情形：只有三种校验全过才算可以复用。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, run  # noqa: E402

add_path("backend")

import instance  # noqa: E402
import paths  # noqa: E402


class DataEnv:
    """把数据根目录临时挪走（含中文与空格），用例之间互不干扰。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="实例测试 ")
        self.root = os.path.join(self._tmp.name, "数据 目录 ADSAgent")
        os.makedirs(self.root)
        self._old = os.environ.get("ADS_AGENT_DATA_DIR")
        os.environ["ADS_AGENT_DATA_DIR"] = self.root
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("ADS_AGENT_DATA_DIR", None)
        else:
            os.environ["ADS_AGENT_DATA_DIR"] = self._old
        try:
            self._tmp.cleanup()
        except OSError:
            pass
        return False


def _payload(service=instance.SERVICE_BACKEND,
             protocol=paths.PROTOCOL_VERSION,
             install_id=None, **extra):
    body = {"service": service, "protocol": protocol}
    if install_id is not None:
        body["identity"] = {
            "install_id": install_id,
            "plugin_version": paths.PLUGIN_VERSION,
            "protocol": protocol,
            "app_root": paths.app_root(),
            "data_root": paths.data_root(),
            "pid": 1234,
        }
        body["identity"].update(extra)
    return {"reachable": True, "payload": body, "error": ""}


def _verdict(probed, kind="backend"):
    return instance.evaluate(probed, kind)


def test_own_backend_is_reusable():
    with DataEnv():
        verdict = _verdict(_payload(install_id=paths.install_id()))
        ok(verdict["usable"], f"自己这份安装的后端必须能复用：{verdict['detail']}")
        eq(verdict["reason"], "ok")
        ok(not verdict["conflict"])


def test_foreign_install_is_rejected():
    with DataEnv():
        verdict = _verdict(_payload(install_id="另一个安装的ID0001"))
        ok(not verdict["usable"], "别的安装不能复用")
        eq(verdict["reason"], "foreign_install")
        ok(verdict["conflict"], "必须标记为冲突")
        contains(verdict["detail"], "另一个 ADS Agent 安装")


def test_old_protocol_is_rejected():
    with DataEnv():
        verdict = _verdict(_payload(protocol=paths.PROTOCOL_VERSION - 1,
                                    install_id=paths.install_id()))
        ok(not verdict["usable"], "协议版本不一致不能复用")
        eq(verdict["reason"], "protocol_mismatch")
        ok(verdict["conflict"])


def test_missing_identity_is_rejected():
    """更旧的后端不上报身份 —— 无法证明归属，按冲突处理（fail closed）。"""
    with DataEnv():
        verdict = _verdict(_payload(install_id=None))
        ok(not verdict["usable"])
        eq(verdict["reason"], "no_identity")
        contains(verdict["detail"], "旧")


def test_foreign_service_is_rejected():
    with DataEnv():
        verdict = _verdict(_payload(service="some_other_app",
                                    install_id=paths.install_id()))
        ok(not verdict["usable"], "端口上是别的程序，不能当后端用")
        eq(verdict["reason"], "foreign_service")
        contains(verdict["detail"], "其它程序")


def test_wrong_kind_service_is_conflict():
    with DataEnv():
        # 后端的口子上答话的却是工具服务 —— 端口配重复了
        verdict = _verdict(_payload(service=instance.SERVICE_TOOLSERVER,
                                    install_id=paths.install_id()), "backend")
        ok(not verdict["usable"])
        eq(verdict["reason"], "wrong_service")


def test_unreachable_is_not_usable():
    with DataEnv():
        verdict = _verdict({"reachable": False, "payload": None, "error": "超时"})
        ok(not verdict["usable"])
        eq(verdict["reason"], "unreachable")
        ok(not verdict["conflict"], "连不上通常不是冲突，重试即可")


def test_non_dict_payload_is_rejected():
    with DataEnv():
        verdict = _verdict({"reachable": True, "payload": None, "error": "格式不对"})
        ok(not verdict["usable"])
        eq(verdict["reason"], "bad_payload")


def test_instance_registry_roundtrip():
    with DataEnv():
        p = instance.write_instance("backend", 8760, host="127.0.0.1",
                                    extra={"python": sys.executable})
        ok(os.path.isfile(p), "应写下登记文件")
        record = instance.read_instance("backend", 8760)
        eq(record["port"], 8760)
        eq(record["install_id"], paths.install_id())
        eq(record["kind"], "backend")
        ok(instance.pid_alive(record["pid"]), "登记里的 pid 应当是自己，且活着")

        listing = instance.list_instances("backend")
        ok(len(listing) >= 1)
        ok(instance.clear_instance("backend", 8760), "应能清掉自己的登记")
        eq(instance.read_instance("backend", 8760), None)


def test_multiple_ports_are_tracked_separately():
    """两个不同端口的登记不能互相覆盖 —— 否则多开检测无从谈起。"""
    with DataEnv():
        instance.write_instance("backend", 8760)
        instance.write_instance("backend", 8765)
        eq(len(instance.list_instances("backend")), 2)
        # 同一 install_id、不同端口 -> 多开冲突
        clashes = instance.same_install_conflicts("backend", 8760)
        eq(len(clashes), 1, "同一份安装的另一个端口实例应被识别为多开")
        eq(clashes[0]["port"], 8765)
        # 自己这个端口不算冲突
        eq(len(instance.same_install_conflicts("backend", 8760) ), 1)
        eq([c for c in instance.same_install_conflicts("backend", 8765)
            if c["port"] == 8765], [])


def test_foreign_install_instances_are_listed():
    with DataEnv():
        instance.write_instance("backend", 8799)
        record = instance.read_instance("backend", 8799)
        # 伪造一条"别的安装"的登记
        other = dict(record)
        other["install_id"] = "别人家的安装ID01"
        other["port"] = 8798
        with open(paths.instance_path("backend", "8798"), "w", encoding="utf-8") as f:
            json.dump(other, f)
        foreigners = instance.foreign_install_instances("backend")
        eq(len(foreigners), 1)
        eq(foreigners[0]["install_id"], "别人家的安装ID01")


def test_stale_registrations_are_cleaned():
    with DataEnv():
        # 一个早就退出的 pid
        dead = dict(instance.identity(), kind="backend", port=8770, pid=999999)
        os.makedirs(paths.runtime_dir(), exist_ok=True)
        with open(paths.instance_path("backend", "8770"), "w", encoding="utf-8") as f:
            json.dump(dead, f)
        cleaned = instance.cleanup_stale_instances()
        ok("backend_8770" in cleaned, f"应清理掉已死进程的登记，实际清理了 {cleaned}")
        ok(not os.path.exists(paths.instance_path("backend", "8770")))


def test_port_in_use_detection():
    import socket

    with DataEnv():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            ok(instance.port_in_use("127.0.0.1", port), "正在监听的端口应判定为占用")
        finally:
            sock.close()
        # 关掉之后应当立刻可用（端口释放）
        ok(not instance.port_in_use("127.0.0.1", port), "关闭后端口应回到空闲")


def test_identity_has_no_secrets():
    with DataEnv():
        ident = instance.identity()
        blob = json.dumps(ident, ensure_ascii=False)
        # install_id 不是令牌；这里断言它确实不含任何凭据字段名
        for key in ("token", "api_key", "apikey", "password", "secret"):
            ok(key not in blob.lower(), f"身份信息里不应出现 {key}")
        ok(ident["install_id"], "install_id 是识别实例归属的唯一依据")
        ne(ident["install_id"], "", "install_id 不能为空")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "实例登记与身份校验"))
