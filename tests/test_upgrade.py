"""升级：换了版本，用户的 API 设置、会话、设计任务必须原样留在原地。

这里走的是**真实的安装流程**（``install_addon.py`` 的 install 分支），只是把
ADS 目录换成了临时副本、数据目录换成了临时位置 —— 真实 ADS 注册文件与真实
%LOCALAPPDATA%\\ADSAgent 全程不被写入。

（``test_registration.py`` 里有一条专门盯"真实注册文件没被改动"的断言。）
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

ROOT = add_path()
add_path("backend")

import install_addon  # noqa: E402
import paths  # noqa: E402

REAL_XML = r"E:\ADS\Programfile\ADS2027\config\eesof_addons.xml"
PLUGIN_ENTRY = install_addon.PLUGIN_ENTRY

#: 刻意写上"用户自己改过的样子" —— 升级后必须一字不差
USER_CONFIG = "\n".join([
    "; 用户改过的配置 —— 升级后必须原样保留",
    "[llm]",
    "base_url = https://用户自己的地址.local/v1",
    "model = 用户选的模型",
    "api_key = FAKE_KEY_FOR_TEST_ONLY_不真能用",
    "",
    "[ads]",
    "host = 127.0.0.1",
    "port = 8761",
    "token = FAKE_TOKEN_FOR_TEST_ONLY_0123456789",
    "",
    "[agent]",
    "max_tool_steps = 42",
    "",
])

USER_SESSIONS = {
    "active": "用户的项目 一号",
    "projects": {
        "用户的项目 一号": {"entries": [{"role": "user", "text": "用户的历史消息"}],
                            "history": []},
    },
}

USER_JOB = {"job_id": "job_upgrade_case_1", "state": "done",
            "spec": {"metrics": [{"name": "增益", "target": 15}]}}


class Scenario:
    """临时 ADS 副本 + 临时数据目录 + 一份"像用户用了一阵子"的数据。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="升级测试 ")
        self.app = os.path.join(self._tmp.name, "程序 目录 ADSAgent")
        self.data = os.path.join(self._tmp.name, "数据 目录 ADSAgent")
        self.ads = os.path.join(self._tmp.name, "ADS 安装目录 2027")
        for d in (self.data, os.path.join(self.ads, "config"),
                  os.path.join(self.ads, "bin"), os.path.join(self.ads, "tools", "python")):
            os.makedirs(d, exist_ok=True)
        if os.path.isfile(REAL_XML):
            with open(REAL_XML, encoding="utf-8") as f:
                body = f.read()
        else:
            body = '<?xml version="1.0" ?>\n<EESof_Addons>\n</EESof_Addons>\n'
        with open(os.path.join(self.ads, "config", "eesof_addons.xml"), "w",
                  encoding="utf-8") as f:
            f.write(body)

        self._env = {k: os.environ.get(k) for k in
                     ("ADS_AGENT_DATA_DIR", "ADS_AGENT_APP_DIR", "ADS_AGENT_CONFIG")}
        os.environ["ADS_AGENT_DATA_DIR"] = self.data
        os.environ["ADS_AGENT_APP_DIR"] = self.app
        os.environ.pop("ADS_AGENT_CONFIG", None)

        self._seed_user_data()
        return self

    def __exit__(self, *exc):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self._tmp.cleanup()
        except OSError:
            pass
        return False

    def _seed_user_data(self):
        os.makedirs(os.path.join(self.data, "design_jobs"), exist_ok=True)
        with open(os.path.join(self.data, "config.ini"), "w", encoding="utf-8") as f:
            f.write(USER_CONFIG)
        with open(os.path.join(self.data, "projects.json"), "w", encoding="utf-8") as f:
            json.dump(USER_SESSIONS, f, ensure_ascii=False)
        with open(os.path.join(self.data, "design_jobs", "job_upgrade_case_1.json"),
                  "w", encoding="utf-8") as f:
            json.dump(USER_JOB, f, ensure_ascii=False)

        # 伪装成"以前装过旧版本"
        with open(os.path.join(self.data, "install_state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({
                "install_id": "旧版本的安装ID01",
                "plugin_version": "0.9.0",
                "data_version": 1,
                "ads_dir": self.ads,
                "app_root": self.app,
            }, f, ensure_ascii=False)

    def read(self, rel: str) -> str:
        with open(os.path.join(self.data, rel), encoding="utf-8") as f:
            return f.read()

    def install(self, *extra) -> tuple[int, list]:
        note: list = []
        argv = ["--ads-dir", self.ads, "--mode", "inplace", *extra]
        code = install_addon.main_into(argv, note)
        return code, note


def _need_monkeypatch():
    """install_addon.main() 会把 note 打印出来；这里要拿到 note 本身。"""
    if hasattr(install_addon, "main_into"):
        return

    def main_into(argv, note):
        args = install_addon.build_parser().parse_args(argv)
        try:
            install_addon.cmd_install(args, note)
        except install_addon.InstallError as e:
            note.append(f"错误：{e}")
            return 2
        return 0

    install_addon.main_into = main_into


def test_upgrade_keeps_user_settings_sessions_and_jobs():
    _need_monkeypatch()
    with Scenario() as sc:
        code, note = sc.install()
        eq(code, 0, "安装应当成功：" + " / ".join(note))

        # 1) 配置：一个字符都不许变（含用户注释、微调过的值、fake 密钥）
        eq(sc.read("config.ini"), USER_CONFIG, "升级不得改动用户的 config.ini")

        # 2) 会话
        sessions = json.loads(sc.read("projects.json"))
        eq(sessions["active"], USER_SESSIONS["active"])
        eq(len(sessions["projects"]), 1, "会话条目数不应变化")

        # 3) 设计任务
        job = json.loads(sc.read("design_jobs/job_upgrade_case_1.json"))
        eq(job["job_id"], USER_JOB["job_id"])
        eq(job["spec"]["metrics"][0]["name"], "增益")

        # 4) 版本被记录下来了，且说明了是从旧版本升上来的
        state = json.loads(sc.read("install_state.json"))
        eq(state["plugin_version"], paths.PLUGIN_VERSION, "应写入当前插件版本")
        joined = " ".join(note)
        contains(joined, "0.9.0", "应识别出旧版本")
        contains(joined, paths.PLUGIN_VERSION, "应显示新版本")
        contains(joined, "均已保留", "必须明确告诉用户数据保留了")


def test_install_state_retains_stable_install_id():
    """install_id 不能因为升级就换一个 —— 它是实例归属的唯一依据。"""
    _need_monkeypatch()
    with Scenario() as sc:
        sc.install()
        state = json.loads(sc.read("install_state.json"))
        eq(state["install_id"], "旧版本的安装ID01", "升级不应更换 install_id")
        eq(state["ads_dir"], sc.ads, "应记录 ADS 安装目录，下次安装可直接复用")
        eq(state["registration_scope"], "installation")


def test_repeated_install_does_not_reset_data():
    _need_monkeypatch()
    with Scenario() as sc:
        sc.install()
        after_first = sc.read("config.ini")
        sc.install()
        eq(sc.read("config.ini"), after_first, "重复安装不得重置用户配置")
        code, note = sc.install("--purge-data")
        eq(code, 0)
        eq(sc.read("config.ini"), after_first, "--purge-data 只在卸载时生效，安装时不该删数据")


def test_install_registers_into_selected_ads_only():
    _need_monkeypatch()
    with Scenario() as sc:
        sc.install()
        xml = os.path.join(sc.ads, "config", "eesof_addons.xml")
        _t, entries, err = install_addon.parse_entries(xml)
        eq(err, "")
        ok(install_addon.ADDON_NAME in entries, "应注册到 --ads-dir 指定的那份配置里")
        # --mode inplace：注册的就是安装器自己所在的那个安装目录
        eq(entries[install_addon.ADDON_NAME][0],
           os.path.join(install_addon._HERE, PLUGIN_ENTRY),
           "就地安装应指向安装器所在的程序目录")


def test_deploy_copies_only_whitelisted_program_files():
    """--mode deploy：复制到目标目录，且**不夹带任何用户数据**。"""
    _need_monkeypatch()
    with Scenario() as sc:
        target = os.path.join(sc._tmp.name, "Programs", "ADSAgent")
        code, note = sc.install("--mode", "deploy", "--install-dir", target)
        eq(code, 0, "安装应当成功：" + " / ".join(note))

        ok(os.path.isfile(os.path.join(target, "addon", "ads_agent", "__init__.py")),
           "必须有插件入口")
        ok(os.path.isfile(os.path.join(target, "backend", "server.py")), "必须有后端")
        ok(os.path.isfile(os.path.join(target, "backend", "paths.py")), "必须有路径模块")
        ok(os.path.isfile(os.path.join(target, "config.example.ini")),
           "必须有干净模板（而不是真实 config.ini）")

        # 用户数据一个都不能出现在程序目录里
        leaked = [
            os.path.join(target, name)
            for name in ("config.ini", "projects.json", "projects.json.bak",
                         "design_jobs", "logs", "tests", "tools", ".git")
            if os.path.exists(os.path.join(target, name))
        ]
        eq(leaked, [], f"程序目录里不该出现用户数据或开发产物：{leaked}")

        xml = os.path.join(sc.ads, "config", "eesof_addons.xml")
        _t, entries, _e = install_addon.parse_entries(xml)
        eq(entries[install_addon.ADDON_NAME][0],
           os.path.join(target, PLUGIN_ENTRY), "deploy 后应注册到新安装目录")


def test_deploy_then_redeploy_is_safe():
    """重复 deploy：已经存在的有效程序文件不应被推倒重来。"""
    _need_monkeypatch()
    with Scenario() as sc:
        target = os.path.join(sc._tmp.name, "Programs2", "ADSAgent")
        sc.install("--mode", "deploy", "--install-dir", target)
        marker = os.path.join(target, "backend", "paths.py")
        stat_before = os.stat(marker)
        sc.install("--mode", "deploy", "--install-dir", target)
        ok(os.path.isfile(marker), "重复 deploy 后文件仍在")
        eq(os.stat(marker).st_size, stat_before.st_size, "内容不应被破坏")


def test_uninstall_keeps_user_data_by_default():
    with Scenario() as sc:
        note: list = []
        args = install_addon.build_parser().parse_args(
            ["--remove", "--ads-dir", sc.ads]
        )
        code = install_addon.cmd_remove(args, note)
        eq(code, 0)
        joined = " ".join(note)
        contains(joined, "已保留", "卸载默认保留用户数据")
        ok(os.path.isfile(os.path.join(sc.data, "config.ini")),
           "卸载后 config.ini 必须还在")
        ok(os.path.isfile(os.path.join(sc.data, "projects.json")),
           "卸载后会话必须还在")
        ok(os.path.isdir(os.path.join(sc.data, "design_jobs")),
           "卸载后设计任务必须还在")
        contains(joined, "--purge-data", "必须告诉用户怎么显式清除")


def test_uninstall_keeps_other_addons():
    """卸载不能把 ADS 自己那十几个插件一起带走。"""
    with Scenario() as sc:
        xml = os.path.join(sc.ads, "config", "eesof_addons.xml")
        _t, before, _e = install_addon.parse_entries(xml)
        install_addon.register(xml, os.path.join(sc.app, PLUGIN_ENTRY))
        note: list = []
        args = install_addon.build_parser().parse_args(["--remove", "--ads-dir", sc.ads])
        install_addon.cmd_remove(args, note)
        _t2, after, _e2 = install_addon.parse_entries(xml)
        for name, attrs in install_addon.entry_snapshot(before).items():
            if name == install_addon.ADDON_NAME:
                continue
            eq(install_addon.entry_snapshot(after).get(name), attrs,
               f"卸载动了官方插件 {name}")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "升级与卸载保留数据"))
